const body = document.body;
const navToggle = document.querySelector("[data-nav-toggle]");
const navCloseButtons = document.querySelectorAll("[data-nav-close]");
const sidebar = document.querySelector(".sidebar");
const mainArea = document.querySelector(".main-area");
const desktopNavigation = window.matchMedia("(min-width: 961px)");

const syncNavigationAccessibility = () => {
  if (!sidebar) return;

  if (desktopNavigation.matches) {
    sidebar.inert = false;
    sidebar.removeAttribute("aria-hidden");
    if (mainArea) mainArea.inert = false;
    return;
  }

  const isOpen = body.classList.contains("nav-open");
  sidebar.inert = !isOpen;
  sidebar.setAttribute("aria-hidden", String(!isOpen));
  if (mainArea) mainArea.inert = isOpen;
};

const closeNavigation = () => {
  body.classList.remove("nav-open");
  navToggle?.setAttribute("aria-expanded", "false");
  syncNavigationAccessibility();
};

navToggle?.addEventListener("click", () => {
  const isOpen = body.classList.toggle("nav-open");
  navToggle.setAttribute("aria-expanded", String(isOpen));
  syncNavigationAccessibility();
});

navCloseButtons.forEach((button) => button.addEventListener("click", closeNavigation));

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeNavigation();
});

desktopNavigation.addEventListener("change", (event) => {
  if (event.matches) closeNavigation();
  else syncNavigationAccessibility();
});

syncNavigationAccessibility();

const updateFileName = (input) => {
  const picker = input.closest("label");
  const nameTarget = picker?.querySelector("[data-file-name]");
  if (!nameTarget) return;

  const fallback = input.name === "file" ? "仅支持 .docx 格式" : "未选择文件";
  nameTarget.textContent = input.files?.[0]?.name || fallback;
  picker.classList.toggle("has-file", Boolean(input.files?.length));
};

document.querySelectorAll("[data-file-input]").forEach((input) => {
  input.addEventListener("change", () => updateFileName(input));
});

document.querySelectorAll(".upload-zone").forEach((zone) => {
  const input = zone.querySelector("input[type='file']");
  if (!input) return;

  ["dragenter", "dragover"].forEach((eventName) => {
    zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      if (!input.disabled) zone.classList.add("is-dragging");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.remove("is-dragging");
    });
  });

  zone.addEventListener("drop", (event) => {
    const droppedFile = event.dataTransfer?.files?.[0];
    if (!droppedFile || input.disabled) return;

    const transfer = new DataTransfer();
    transfer.items.add(droppedFile);
    input.files = transfer.files;
    updateFileName(input);
  });
});

document.querySelectorAll("[data-loading-form]").forEach((form) => {
  if (document.querySelector(".task-state-running")) {
    const button = form.querySelector("button[type='submit']");
    if (button) button.disabled = true;
    return;
  }

  form.addEventListener("submit", () => {
    const button = form.querySelector("button[type='submit']");
    if (!button) return;
    button.textContent = button.dataset.loadingLabel || "处理中…";
    button.disabled = true;
  });
});

if (document.querySelector(".task-state-running")) {
  const params = new URLSearchParams(window.location.search);
  const ctx = params.get("ctx");
  const statusUrl = ctx
    ? `/web/review/status?ctx=${encodeURIComponent(ctx)}`
    : "/web/review/status";

  const checkReviewStatus = async () => {
    try {
      const response = await fetch(statusUrl, {
        cache: "no-store",
        credentials: "same-origin",
      });
      if (response.status === 401) {
        window.location.replace("/web/login");
        return;
      }
      if (!response.ok) return;

      const data = await response.json();
      if (["succeeded", "failed", "cancelled"].includes(data.status)) {
        window.location.reload();
      }
    } catch {
      // A temporary status-request failure must not disrupt the active page.
    }
  };

  window.setInterval(checkReviewStatus, 5000);
}

document.querySelectorAll("[data-confirm-delete]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    const username = form.dataset.confirmUsername || "该用户";
    const first = window.confirm(`确定删除用户 ${username}？删除后该账号将无法登录。`);
    if (!first) {
      event.preventDefault();
      return;
    }

    const keepDataInput = form.querySelector("input[name='keep_data']");
    const keepsData = keepDataInput?.checked;
    const dataMessage = keepsData ? "用户数据目录会被保留。" : "用户数据目录也会被删除。";
    if (!window.confirm(`${dataMessage}\n再次确认删除用户 ${username}？`)) {
      event.preventDefault();
    }
  });
});

if (body.dataset.authCheck === "true") {
  const checkSession = async () => {
    try {
      const params = new URLSearchParams(window.location.search);
      const ctx = params.get("ctx");
      const statusUrl = ctx ? `/web/session/status?ctx=${encodeURIComponent(ctx)}` : "/web/session/status";
      const response = await fetch(statusUrl, {
        cache: "no-store",
        credentials: "same-origin",
      });
      if (response.status === 401) {
        window.location.replace("/web/login");
      }
    } catch {
      // Keep the current page usable when the session probe cannot reach the server.
    }
  };

  window.setInterval(checkSession, 10000);
}
