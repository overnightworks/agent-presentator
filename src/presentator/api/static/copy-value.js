document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", () => {
    const value = button.previousElementSibling.textContent;
    if (!value) return;
    navigator.clipboard.writeText(value).catch((error) => {
      console.error("Copying to the clipboard failed", error);
    });
  });
});
