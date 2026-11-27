// Runs at the start of every document in the Board window, before any script of the page.
// In the Board's own page (the top document) it does nothing. In any frame, which is a
// Canvas page or something a Canvas page made, it takes away what the page's policy cannot
// stop: WebRTC (it sends packets to any server; no policy directive governs it), sound and
// speech (this is a voice assistant: a page must not talk to the room), and the old
// clipboard command.
(() => {
  let framed;
  try {
    framed = window.top !== window;
  } catch {
    framed = true;
  }
  if (!framed) return;
  const gone = [
    "RTCPeerConnection",
    "webkitRTCPeerConnection",
    "RTCDataChannel",
    "RTCSessionDescription",
    "RTCIceCandidate",
    "AudioContext",
    "webkitAudioContext",
    "OfflineAudioContext",
    "webkitOfflineAudioContext",
    "speechSynthesis",
    "SpeechSynthesisUtterance",
    "SpeechRecognition",
    "webkitSpeechRecognition",
    "Audio",
    "MediaStreamTrackGenerator",
    "MediaStreamTrackProcessor",
    "AudioData",
    "AudioDecoder",
    "AudioEncoder",
    "AudioWorkletNode",
    "MediaRecorder",
  ];
  for (const name of gone) {
    try {
      Object.defineProperty(window, name, { value: undefined, writable: false, configurable: false });
    } catch {
      try {
        delete window[name];
      } catch {
        // nothing more to try
      }
    }
  }
  // No element may play: there are ways to feed one that a policy does not see.
  try {
    Object.defineProperty(HTMLMediaElement.prototype, "play", {
      value: () => Promise.reject(new DOMException("not allowed here", "NotAllowedError")),
      writable: false,
      configurable: false,
    });
  } catch {
    // nothing more to try
  }
  try {
    Object.defineProperty(Document.prototype, "execCommand", {
      value: () => false,
      writable: false,
      configurable: false,
    });
  } catch {
    // nothing more to try
  }
})();
