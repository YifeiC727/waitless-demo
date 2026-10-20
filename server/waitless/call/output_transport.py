"""Awaitable downstream discard for the pinned pipecat 1.12.0 sender.

The sender's interruption skips half-blocks when it has never spoken. Clear
those and both resampler histories before acknowledging a new output epoch.
"""
import asyncio
from contextvars import ContextVar
from pipecat.frames.frames import InterruptionFrame, OutputAudioRawFrame, OutputDTMFFrame, OutputDTMFUrgentFrame, LLMFullResponseStartFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.websocket.fastapi import FastAPIWebsocketOutputTransport, FastAPIWebsocketTransport


class ClearingWebsocketOutputTransport(FastAPIWebsocketOutputTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ingress_lock = asyncio.Lock()
        self._gate = None
        self._write_epoch = ContextVar('line_sender_epoch', default=None)
        self._sender_response = ContextVar('line_response_identity', default=None)

    def bind_gate(self, gate):
        self._gate = gate
        gate.requires_output_ack = True
        gate.set_output_cleaner(self.clear_output)

    async def clear_output(self):
        # Cancel queued/in-progress processor data as well as MediaSender data.
        # Do this before acquiring ingress: a data task may hold that lock.
        frame = InterruptionFrame()
        await FrameProcessor.process_frame(self, frame, FrameDirection.DOWNSTREAM)
        token = self._write_epoch.set(self._gate.output_epoch if self._gate else None)
        try:
            async with self._ingress_lock:
                for sender in self._media_senders.values():
                    await sender.handle_interruptions(frame)
                    await sender._cancel_audio_task()
                    sender._clear_audio_buffer()
                    await sender._resampler.reset()
                    # The new sender task inherits this immutable epoch. Sliced
                    # frames no longer carry the original service attributes.
                    sender._create_audio_task()
                self._audio_send_buffer.clear()
                sent = await self._write_frame(frame)
                if not sent and (self._gate is None or self._gate.output_state != 'ended'):
                    raise RuntimeError('line output clear failed')
                self._next_send_time = 0
        finally:
            self._write_epoch.reset(token)

    async def write_transport_frame(self, frame):
        # Control frames retain their service identity through the sender FIFO.
        # Remember it in this sender task, rather than relabelling sliced PCM
        # from whichever response happens to be current at write time.
        if isinstance(frame, LLMFullResponseStartFrame):
            self._sender_response.set(getattr(frame, 'response_identity', None))
        await super().write_transport_frame(frame)

    async def _write_frame(self, frame):
        identity = self._sender_response.get()
        result = await super()._write_frame(frame)
        if (result and isinstance(frame, OutputAudioRawFrame)
                and getattr(frame, '_waitless_audio_packet', False) and any(frame.audio)
                and self._gate is not None and self._write_epoch.get() == self._gate.output_epoch):
            await self._gate.mark_audible(identity)
        return result

    async def write_audio_frame(self, frame):
        epoch = self._write_epoch.get()
        if self._gate is not None and epoch != self._gate.output_epoch:
            return False
        return await super().write_audio_frame(frame)

    async def process_frame(self, frame, direction):
        if isinstance(frame, InterruptionFrame):
            await self.clear_output()
            await self.push_frame(frame, direction)
            return
        token = self._write_epoch.set(self._gate.output_epoch if self._gate else None)
        try:
            if isinstance(frame, (OutputAudioRawFrame, OutputDTMFFrame, OutputDTMFUrgentFrame)):
                async with self._ingress_lock:
                    if self._gate is not None and getattr(frame, '_waitless_output_epoch', None) != self._gate.output_epoch:
                        return
                    await super().process_frame(frame, direction)
                return
            await super().process_frame(frame, direction)
        finally:
            self._write_epoch.reset(token)


class ClearingWebsocketTransport(FastAPIWebsocketTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._output = ClearingWebsocketOutputTransport(self, self._client, self._params, name=self._output_name)
