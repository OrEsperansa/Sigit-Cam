const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

window.sigitFetch = function sigitFetch(url, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  return fetch(url, { ...options, headers });
};

document.querySelectorAll("[data-logout]").forEach((button) => {
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await window.sigitFetch("/logout", { method: "POST" });
    } finally {
      window.location.assign("/login");
    }
  });
});
