document.querySelectorAll("[data-history-back]").forEach((link) => {
  link.addEventListener("click", (event) => {
    if (history.length > 1) {
      event.preventDefault();
      history.back();
    }
  });
});
