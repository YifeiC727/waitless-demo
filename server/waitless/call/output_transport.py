"""Awaitable downstream discard for the pinned pipecat 1.12.0 sender.

The sender's interruption skips half-blocks when it has never spoken. Clear
those and both resampler histories before acknowledging a new output epoch.
"""
import asyncio
from pipecat.frames.frames import InterruptionFrame, OutputAudioRawFrame, OutputDTMFFrame, OutputDTMFUrgentFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.websocket.fastapi import FastAPIWebsocketOutputTransport, FastAPIWebsocketTransport


class ClearingWebsocketOutputTransport(FastAPIWebsocketOutputTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ingress_lock = asyncio.Lock()
        self._gate = None

    def bind_gate(self, gate):
        self._gate = gate
        gate.set_output_cleaner(self.clear_output)

    async def clear_output(self):
        async with self._ingress_lock:
            # handle_interruptions cancels active sends, including a websocket
            # write, before clear. No previous sender can write after return.
            frame = InterruptionFrame()
            for sender in self._media_senders.values():
                await sender.handle_interruptions(frame)
                await sender._cancel_audio_task()
                sender._clear_audio_buffer()
                await sender._resampler.reset()
                sender._create_audio_task()
            self._audio_send_buffer.clear()
            # LineSerializer resets its output filter before producing clear.
            if not await self._write_frame(frame):
                raise RuntimeError('line output clear failed')
            self._next_send_time = 0

    async def process_frame(self, frame, direction):
        if isinstance(frame, InterruptionFrame):
            await self.clear_output()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, (OutputAudioRawFrame, OutputDTMFFrame, OutputDTMFUrgentFrame)):
            async with self._ingress_lock:
                if self._gate is not None and getattr(frame, '_waitless_output_epoch', None) != self._gate.output_epoch:
                    return
                await super().process_frame(frame, direction)
            return
        await super().process_frame(frame, direction)


class ClearingWebsocketTransport(FastAPIWebsocketTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._output = ClearingWebsocketOutputTransport(self, self._client, self._params, name=self._output_name)
