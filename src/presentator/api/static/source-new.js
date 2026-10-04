(() => {
  const create = document.querySelector("[data-create]");
  const proven = create.dataset.proven === "true";
  document.querySelectorAll('input[name="access"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      create.disabled = !proven;
    });
  });
})();
