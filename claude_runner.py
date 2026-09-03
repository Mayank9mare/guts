import asyncio
import json
from dataclasses import dataclass

from config import CLAUDE_CLI, SUBPROCESS_TIMEOUT


@dataclass
class ClaudeEvent:
    """Parsed event from claude stream-json output."""
    raw_type: str       # "system", "assistant", "user", "result", "rate_limit_event"
    subtype: str | None  # e.g. "init", "success"
    session_id: str | None
    content_type: str | None  # "text", "tool_use", "tool_result"
    tool_name: str | None
    tool_input: dict | None
    text: str | None
    result: str | None
    is_error: bool
    raw: dict


def parse_event(line: str) -> ClaudeEvent | None:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None

    raw_type = data.get("type", "")
    subtype = data.get("subtype")
    session_id = data.get("session_id")
    content_type = None
    tool_name = None
    tool_input = None
    text = None
    result = None
    is_error = data.get("is_error", False)

    if raw_type == "assistant":
        message = data.get("message", {})
        content_blocks = message.get("content", [])
        for block in content_blocks:
            if block.get("type") == "text":
                content_type = "text"
                text = block.get("text", "")
            elif block.get("type") == "tool_use":
                content_type = "tool_use"
                tool_name = block.get("name", "")
                tool_input = block.get("input", {})

    elif raw_type == "user":
        message = data.get("message", {})
        content_blocks = message.get("content", [])
        for block in content_blocks:
            if block.get("type") == "tool_result":
                content_type = "tool_result"
                text = block.get("content", "")
                is_error = block.get("is_error", False)
        # Also check tool_use_result for richer info
        tool_result = data.get("tool_use_result")
        if isinstance(tool_result, dict):
            stdout = tool_result.get("stdout", "")
            stderr = tool_result.get("stderr", "")
            text = stderr if stderr else stdout

    elif raw_type == "result":
        result = data.get("result", "")

    return ClaudeEvent(
        raw_type=raw_type,
        subtype=subtype,
        session_id=session_id,
        content_type=content_type,
        tool_name=tool_name,
        tool_input=tool_input,
        text=text,
        result=result,
        is_error=is_error,
        raw=data,
    )


class ClaudeRunner:
    def __init__(self):
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def run(self, prompt: str, session_id: str, cwd: str, model: str, resume: bool = False, allowed_tools: str | None = None, system_prompt: str | None = None, disallowed_tools: str | None = None):
        """
        Run claude CLI and yield ClaudeEvent objects.
        If resume=True, uses --resume instead of --session-id.
        If allowed_tools is set, restricts Claude to those tools only.
        """
        cmd = [
            CLAUDE_CLI, "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model", model,
            "--permission-mode", "bypassPermissions",
        ]

        if allowed_tools:
            cmd.extend(["--allowedTools", allowed_tools])

        if disallowed_tools:
            cmd.extend(["--disallowedTools", disallowed_tools])

        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        if resume:
            cmd.extend(["--resume", session_id])
        else:
            cmd.extend(["--session-id", session_id])

        import logging
        logging.getLogger(__name__).info(f"Claude cmd: resume={resume} session={session_id[:8]} model={model}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            limit=10 * 1024 * 1024,  # 10MB buffer for large stream-json lines (PR diffs etc.)
        )

        self._processes[session_id] = process

        # Send prompt via stdin
        if process.stdin:
            process.stdin.write(prompt.encode() + b"\n")
            await process.stdin.drain()
            process.stdin.close()

        try:
            got_events = False
            async for event in self._read_events(process, session_id):
                got_events = True
                yield event
            if not got_events:
                # No events — check stderr
                stderr_data = b""
                if process.stderr:
                    try:
                        stderr_data = await asyncio.wait_for(process.stderr.read(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
                exit_code = process.returncode
                import logging
                logging.getLogger(__name__).error(f"Claude subprocess produced no events. exit_code={exit_code} stderr={stderr_data.decode()[:500]}")
                yield ClaudeEvent(
                    raw_type="error", subtype="no_output", session_id=session_id,
                    content_type=None, tool_name=None, tool_input=None,
                    text=f"Claude exited with no output (code={exit_code}). stderr: {stderr_data.decode()[:300]}",
                    result=None, is_error=True, raw={},
                )
        finally:
            self._processes.pop(session_id, None)

    async def _read_events(self, process, session_id: str):
        assert process.stdout is not None
        timeout_task = asyncio.create_task(asyncio.sleep(SUBPROCESS_TIMEOUT))

        try:
            while True:
                read_task = asyncio.create_task(process.stdout.readline())
                done, _ = await asyncio.wait(
                    {read_task, timeout_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if timeout_task in done:
                    read_task.cancel()
                    process.terminate()
                    yield ClaudeEvent(
                        raw_type="error", subtype="timeout", session_id=session_id,
                        content_type=None, tool_name=None, tool_input=None,
                        text=f"Session timed out after {SUBPROCESS_TIMEOUT}s",
                        result=None, is_error=True, raw={},
                    )
                    return

                line = read_task.result()
                if not line:
                    break

                decoded = line.decode().strip()
                if not decoded:
                    continue

                event = parse_event(decoded)
                if event:
                    yield event

                    if event.raw_type == "result":
                        return
        finally:
            timeout_task.cancel()
            if process.returncode is None:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    process.kill()

    async def kill_session(self, session_id: str) -> bool:
        process = self._processes.get(session_id)
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
            self._processes.pop(session_id, None)
            return True
        return False
