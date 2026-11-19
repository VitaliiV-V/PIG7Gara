(() => {
    const form = document.getElementById("suggestion-form");
    if (!form || form.classList.contains("hidden")) return;
    const text = form.elements.text;
    const count = document.getElementById("character-count");
    const error = document.getElementById("submission-error");
    const button = form.querySelector("button[type=submit]");
    const updateCount = () => { count.textContent = `${text.value.length} / 3000`; };
    text.addEventListener("input", updateCount);
    updateCount();

    form.addEventListener("submit", async event => {
        event.preventDefault();
        if (button.disabled) return;
        error.classList.add("hidden");
        button.disabled = true;
        button.textContent = "Отправляем…";
        try {
            const response = await fetch(form.action, {
                method: "POST",
                body: new URLSearchParams(new FormData(form)),
                headers: { Accept: "application/json" },
                credentials: "same-origin",
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || "Не удалось отправить заявку. Попробуйте позже.");
            document.getElementById("receipt").textContent = result.receipt;
            text.value = "";
            form.classList.add("hidden");
            const success = document.getElementById("submission-success");
            success.classList.remove("hidden");
            success.tabIndex = -1;
            success.focus();
        } catch (failure) {
            error.textContent = failure instanceof SyntaxError || failure instanceof TypeError
                ? "Не удалось получить ответ. Текст сохранён в форме — повторите отправку."
                : failure.message;
            error.classList.remove("hidden");
        } finally {
            button.disabled = false;
            button.textContent = "Отправить на модерацию";
        }
    });
})();
