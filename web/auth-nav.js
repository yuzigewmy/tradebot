(() => {
  const nav = document.querySelector(".workspace-nav");
  if (!nav) return;

  fetch("/api/auth/status", { cache: "no-store" })
    .then((res) => (res.ok ? res.json() : null))
    .then((data) => {
      if (!data || !data.enabled || !data.authenticated) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "nav-logout";
      btn.textContent = "退出";
      btn.title = data.user ? `退出登录 (${data.user})` : "退出登录";
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await fetch("/api/auth/logout", { method: "POST" });
        } finally {
          window.location.replace("/login");
        }
      });
      nav.appendChild(btn);
    })
    .catch(() => {});
})();
