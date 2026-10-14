"""Nova 2 requires an interactive user message to begin speaking."""
import asyncio

from pipecat.frames.frames import LLMContextFrame, OutputAudioRawFrame, LLMFullResponseEndFrame

from tests.lane_B.test_company_bot_run import Sonic, launch, setup


async def test_nova2_bot_starts_greeting_without_caller_audio(setup, monkeypatch):
    from waitless.company import bot

    class Nova2(Sonic):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, LLMContextFrame):
                # pipecat 1.12.0 Nova Sonic sends the last USER message as
                # interactive input; system-only history cannot start a turn.
                messages = frame.context.get_messages()
                if messages and messages[-1]["role"] == "user" and messages[-1]["content"]:
                    await self.push_frame(OutputAudioRawFrame(b"\x40\x03" * 160, 8000, 1))
                    await self.push_frame(LLMFullResponseEndFrame())

    instances = setup[2]

    def factory(**kwargs):
        service = Nova2(**kwargs)
        instances.append(service)
        return service

    monkeypatch.setattr(bot, "_llm_factory", factory)
    socket, task, service, _, _ = await launch(setup)
    try:
        async with asyncio.timeout(1):
            while not socket.sent:
                await asyncio.sleep(.01)
        assert not service.audio, "the company should greet before caller audio arrives"
    finally:
        await socket.incoming.put(None)
        await asyncio.wait_for(task, 3)
