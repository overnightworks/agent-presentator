(() => {
  const retryMilliseconds = 1000;
  const player = document.querySelector("[data-sample-player]");
  const action = player.querySelector("[data-sample-action]");
  const status = player.querySelector("[data-sample-status]");
  const failure = player.querySelector("[data-sample-failure]");
  const languages = [...player.querySelectorAll("[data-sample-language]")];
  let language = "de";
  let retry;
  let request;
  let audio;
  let objectUrl;

  const stopAudio = () => {
    if (audio) {
      audio.pause();
      audio = undefined;
    }
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = undefined;
    }
  };
  const show = (state) => {
    failure.hidden = state !== "failed";
    status.hidden = state !== "loading" && state !== "waiting";
    status.textContent = state === "waiting" ? player.dataset.waiting : player.dataset.loading;
    action.disabled = state === "loading" || state === "waiting";
    action.textContent = state === "playing" ? player.dataset.stop : player.dataset.play;
  };
  const play = async () => {
    window.clearTimeout(retry);
    show("loading");
    request = new AbortController();
    try {
      const response = await fetch(
        `/settings/voice/${player.dataset.voice}/sample/${language}`,
        { method: "POST", signal: request.signal },
      );
      if (response.status === 409) {
        show("waiting");
        retry = window.setTimeout(play, retryMilliseconds);
        return;
      }
      if (!response.ok) throw new Error("sample unavailable");
      objectUrl = URL.createObjectURL(await response.blob());
      audio = new Audio(objectUrl);
      audio.addEventListener("ended", () => {
        stopAudio();
        show("idle");
      });
      await audio.play();
      show("playing");
    } catch {
      if (request.signal.aborted) return;
      stopAudio();
      show("failed");
    }
  };

  languages.forEach((choice) => choice.addEventListener("click", () => {
    language = choice.dataset.sampleLanguage;
    languages.forEach((other) => other.setAttribute("aria-pressed", String(other === choice)));
  }));
  action.addEventListener("click", () => {
    if (audio) {
      stopAudio();
      show("idle");
      return;
    }
    void play();
  });
  window.addEventListener("pagehide", () => {
    window.clearTimeout(retry);
    request?.abort();
  });
})();
