document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", () => {
    const value = button.previousElementSibling.textContent;
    if (value) navigator.clipboard.writeText(value);
  });
});
