(() => {
  const isMemberCookie = /(?:^|;\s*)tb_role=member(?:;|$)/.test(document.cookie);
  if (isMemberCookie) {
    document.documentElement.classList.add("role-member");
  }

  const nav = document.querySelector(".workspace-nav");
  if (!nav) return;

  fetch("/api/auth/status", { cache: "no-store" })
    .then((res) => (res.ok ? res.json() : null))
    .then((data) => {
      if (!data) return;
      const accountLink = nav.querySelector('a[href="/account"]');
      if (data.can_view_account === false) {
        document.documentElement.classList.add("role-member");
        if (accountLink) accountLink.remove();
      } else {
        document.documentElement.classList.remove("role-member");
      }

      if (!data.enabled || !data.authenticated) return;
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
