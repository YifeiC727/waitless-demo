# Voice input integration

The chat composer includes a microphone, timer, animated indicator, cancel, and finish controls. Completed transcripts are appended to the draft; users edit and send them themselves. Cancel preserves the original draft. Escape, leaving the page, restarting, opening a Companion screen, or hiding the page stops capture. Recording is limited to 60 seconds and transcription requests time out after 30 seconds.

## Default demo

Without configuration, voice input is explicitly simulated: it never requests microphone permission or records audio and inserts the Costco-cancellation example when finished. GitHub Pages currently uses this mode.

## Connect an HTTP transcription service

Set the endpoint after `window.WaitlessVoice` is initialized:

```js
window.WaitlessVoice.endpoint = 'https://your-backend.example/api/transcribe';
```

On the user's microphone click, the browser requests microphone permission and captures audio using MediaRecorder. Finishing uploads multipart form data:

- Method: `POST`
- Field: `audio` (audio file; MIME type varies by browser)
- Success response: `{ "text": "recognized transcript" }`
- CORS must allow the frontend origin when the backend is on another origin.

Use HTTPS or localhost. Keep provider API keys on the backend. The included Node demo server does not implement this endpoint; configure your own service to activate real transcription. No credentials or recordings are included in the MCP trace.

## Custom adapter

Alternatively set a custom function that receives the audio Blob and AbortSignal and returns a transcript string:

```js
window.WaitlessVoice.transcribe = async (blob, { signal }) => {
  const form = new FormData();
  form.append('audio', blob, 'voice');
  const response = await fetch('/api/transcribe', {
    method: 'POST', body: form, signal
  });
  if (!response.ok) throw new Error('Could not transcribe. Please try again.');
  return (await response.json()).text;
};
```

The custom adapter takes precedence over `endpoint` and should honor `signal`. Permission denial, absent microphones, unsupported recording, empty transcripts, request failures, and timeouts return the composer to text entry with a status message.

For the capture APIs see https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder and https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia.
