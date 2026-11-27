(() => {
  const form = document.getElementById("login-form");
  const usernameInput = document.getElementById("username");
  const passwordInput = document.getElementById("password");
  const rememberInput = document.getElementById("remember");
  const toggleBtn = document.getElementById("toggle-password");
  const eyeIcon = toggleBtn.querySelector(".icon-eye");
  const eyeOffIcon = toggleBtn.querySelector(".icon-eye-off");
  const capsBadge = document.getElementById("caps-badge");
  const errorBox = document.getElementById("auth-error");
  const errorText = document.getElementById("auth-error-text");
  const submitBtn = document.getElementById("submit-btn");
  const submitSpinner = document.getElementById("submit-spinner");
  const submitCheck = document.getElementById("submit-check");
  const submitLabel = document.getElementById("submit-label");
  const submitArrow = document.getElementById("submit-arrow");
  const stageStatus = document.getElementById("stage-status");
  const stageStatusLabel = document.getElementById("stage-status-label");
  const mobileStatusLabel = document.getElementById("mobile-status-label");
  const coordLabel = document.getElementById("stage-coord");

  let countdownTimer = null;
  let focusBoost = 0;

  const params = new URLSearchParams(window.location.search);
  const nextTarget = params.get("next") || "";

  // Fetch execution mode badge without exposing private account metrics
  async function loadStatus() {
    try {
      const res = await fetch("/api/auth/status", { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      if (data.authenticated && data.enabled) {
        window.location.replace(nextTarget.startsWith("/") && !nextTarget.startsWith("//") ? nextTarget : "/account");
        return;
      }
      const modeMap = {
        live: { text: "BINANCE LIVE · 实盘巡航中", mobile: "Binance 实盘控制台", live: true },
        demo: { text: "BINANCE DEMO · 模拟环境", mobile: "Binance 模拟控制台", live: false },
        off: { text: "RESEARCH DESK · 本地研究模式", mobile: "加密研究台", live: false },
      };
      const meta = modeMap[data.execution_mode] || modeMap.off;
      stageStatusLabel.textContent = meta.text;
      mobileStatusLabel.textContent = meta.mobile;
      stageStatus.classList.toggle("is-live", meta.live);
    } catch {
      stageStatusLabel.textContent = "TRADEBOT CONSOLE · READY";
    }
  }

  function showError(message) {
    errorText.textContent = message;
    errorBox.hidden = false;
    usernameInput.closest(".input-wrap").classList.add("has-error");
    passwordInput.closest(".input-wrap").classList.add("has-error");
    form.classList.remove("is-shaking");
    void form.offsetWidth;
    form.classList.add("is-shaking");
  }

  function clearError() {
    if (countdownTimer) {
      clearInterval(countdownTimer);
      countdownTimer = null;
    }
    errorBox.hidden = true;
    errorText.textContent = "";
    usernameInput.closest(".input-wrap").classList.remove("has-error");
    passwordInput.closest(".input-wrap").classList.remove("has-error");
  }

  function setButtonState(state) {
    submitBtn.classList.remove("is-success");
    if (state === "loading") {
      submitBtn.disabled = true;
      submitSpinner.hidden = false;
      submitCheck.hidden = true;
      submitArrow.hidden = true;
      submitLabel.textContent = "正在验证凭证…";
    } else if (state === "success") {
      submitBtn.disabled = true;
      submitBtn.classList.add("is-success");
      submitSpinner.hidden = true;
      submitCheck.hidden = false;
      submitArrow.hidden = true;
      submitLabel.textContent = "验证通过，正在进入…";
    } else {
      submitBtn.disabled = false;
      submitSpinner.hidden = true;
      submitCheck.hidden = true;
      submitArrow.hidden = false;
      submitLabel.textContent = "进入研究台";
    }
  }

  function startRateCountdown(seconds) {
    let remaining = seconds;
    submitBtn.disabled = true;
    const render = () => {
      errorText.textContent = `连续尝试次数过多，请 ${remaining} 秒后再试。`;
      errorBox.hidden = false;
    };
    render();
    countdownTimer = setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        clearInterval(countdownTimer);
        countdownTimer = null;
        clearError();
        setButtonState("idle");
        passwordInput.focus();
      } else {
        render();
      }
    }, 1000);
  }

  // Toggle password visibility
  toggleBtn.addEventListener("click", () => {
    const showing = passwordInput.type === "text";
    passwordInput.type = showing ? "password" : "text";
    toggleBtn.setAttribute("aria-pressed", String(!showing));
    toggleBtn.setAttribute("aria-label", showing ? "显示密码" : "隐藏密码");
    eyeIcon.hidden = !showing;
    eyeOffIcon.hidden = showing;
    passwordInput.focus();
  });

  // CapsLock detection on password field
  function updateCapsLock(event) {
    if (typeof event.getModifierState === "function") {
      capsBadge.hidden = !event.getModifierState("CapsLock");
    }
  }
  passwordInput.addEventListener("keydown", updateCapsLock);
  passwordInput.addEventListener("keyup", updateCapsLock);
  passwordInput.addEventListener("blur", () => { capsBadge.hidden = true; });

  [usernameInput, passwordInput].forEach((input) => {
    input.addEventListener("input", () => {
      if (!countdownTimer) clearError();
    });
    input.addEventListener("focus", () => { focusBoost = 1; });
    input.addEventListener("blur", () => { focusBoost = 0; });
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (countdownTimer) return;

    const username = usernameInput.value.trim();
    const password = passwordInput.value;
    if (!username) {
      showError("请输入管理员账号。");
      usernameInput.focus();
      return;
    }
    if (!password) {
      showError("请输入访问密码。");
      passwordInput.focus();
      return;
    }

    clearError();
    setButtonState("loading");

    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username,
          password,
          remember: rememberInput.checked,
          next: nextTarget || null,
        }),
      });
      const payload = await res.json().catch(() => ({}));
      if (res.ok && payload.ok) {
        setButtonState("success");
        const destination = payload.redirect || "/account";
        setTimeout(() => {
          window.location.replace(destination);
        }, 260);
        return;
      }

      if (res.status === 429 && payload.error && payload.error.retry_after) {
        setButtonState("idle");
        startRateCountdown(Number(payload.error.retry_after));
        return;
      }

      const msg = (payload.error && payload.error.message) || "登录失败，请稍后重试。";
      setButtonState("idle");
      showError(msg);
      passwordInput.select();
    } catch {
      setButtonState("idle");
      showError("无法连接到服务器，请检查网络后重试。");
    }
  });

  // Subtle calibrated 3-engine harmonic regime contour on the obsidian architectural stage
  function initRegimeCanvas() {
    const canvas = document.getElementById("regime-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let width = 0;
    let height = 0;
    let pointerX = null;
    let pointerY = null;
    let phase = 0;
    let currentBoost = 0;
    let rafId = null;

    const curves = [
      { color: "rgba(214, 152, 92, 0.92)", glow: "rgba(214, 152, 92, 0.14)", width: 1.75, amp: 0.24, freq: 1.55, speed: 0.0055, offset: 0.0, fill: true },
      { color: "rgba(96, 184, 154, 0.86)", glow: "rgba(96, 184, 154, 0.09)", width: 1.45, amp: 0.17, freq: 2.15, speed: 0.0042, offset: 1.9, fill: false },
      { color: "rgba(205, 198, 190, 0.45)", glow: "rgba(205, 198, 190, 0.05)", width: 1.15, amp: 0.12, freq: 1.15, speed: 0.0031, offset: 3.4, fill: false },
    ];

    function resize() {
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = rect.width;
      height = rect.height;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw();
    }

    function evalCurve(curve, xNorm, t) {
      const waveA = Math.sin(xNorm * Math.PI * 2 * curve.freq + t + curve.offset);
      const waveB = Math.sin(xNorm * Math.PI * 2 * (curve.freq * 0.48) - t * 0.65 + curve.offset * 1.4) * 0.45;
      const drift = (xNorm - 0.5) * 0.22;
      return (waveA * 0.65 + waveB * 0.35 + drift) * curve.amp;
    }

    function draw() {
      if (!width || !height) return;
      ctx.clearRect(0, 0, width, height);

      const padTop = 22;
      const padBottom = 34;
      const padLeft = 14;
      const padRight = 56;
      const plotW = Math.max(10, width - padLeft - padRight);
      const plotH = Math.max(10, height - padTop - padBottom);
      const midY = padTop + plotH * 0.52;

      // Calibrated horizontal regime reference lines
      ctx.strokeStyle = "rgba(235, 228, 218, 0.075)";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 3; i += 1) {
        const y = Math.round(padTop + (plotH / 3) * i) + 0.5;
        ctx.beginPath();
        ctx.moveTo(padLeft, y);
        ctx.lineTo(width - padRight, y);
        ctx.stroke();
      }

      // Dashed Donchian breakout envelope around primary BTC curve
      const samples = 96;
      ctx.save();
      ctx.setLineDash([3, 5]);
      ctx.strokeStyle = "rgba(214, 152, 92, 0.22)";
      ctx.lineWidth = 1;
      for (const band of [-0.15, 0.15]) {
        ctx.beginPath();
        for (let i = 0; i <= samples; i += 1) {
          const u = i / samples;
          const x = padLeft + u * plotW;
          const y = midY - (evalCurve(curves[0], u, phase) + band) * plotH;
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.stroke();
      }
      ctx.restore();

      // Render 3 independent engine contours
      curves.forEach((curve, idx) => {
        const pts = [];
        for (let i = 0; i <= samples; i += 1) {
          const u = i / samples;
          const x = padLeft + u * plotW;
          const y = midY - evalCurve(curve, u, phase * (1 + idx * 0.12)) * plotH;
          pts.push([x, y]);
        }

        if (curve.fill) {
          const grad = ctx.createLinearGradient(0, padTop, 0, padTop + plotH);
          grad.addColorStop(0, "rgba(214, 152, 92, 0.13)");
          grad.addColorStop(1, "rgba(214, 152, 92, 0.0)");
          ctx.beginPath();
          ctx.moveTo(pts[0][0], padTop + plotH);
          pts.forEach(([x, y]) => ctx.lineTo(x, y));
          ctx.lineTo(pts[pts.length - 1][0], padTop + plotH);
          ctx.closePath();
          ctx.fillStyle = grad;
          ctx.fill();
        }

        ctx.beginPath();
        pts.forEach(([x, y], i) => {
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.strokeStyle = curve.color;
        ctx.lineWidth = curve.width + currentBoost * 0.25;
        ctx.stroke();

        // Crisp terminal endpoint beacon
        const [lx, ly] = pts[pts.length - 1];
        ctx.beginPath();
        ctx.arc(lx, ly, 3.2, 0, Math.PI * 2);
        ctx.fillStyle = curve.color;
        ctx.fill();
      });

      // Interactive pointer crosshair readout
      if (pointerX !== null && pointerX >= padLeft && pointerX <= padLeft + plotW) {
        const u = (pointerX - padLeft) / plotW;
        ctx.strokeStyle = "rgba(235, 228, 218, 0.18)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(Math.round(pointerX) + 0.5, padTop);
        ctx.lineTo(Math.round(pointerX) + 0.5, padTop + plotH);
        ctx.stroke();

        const btcVal = evalCurve(curves[0], u, phase) * 5.2;
        const ethVal = evalCurve(curves[1], u, phase * 1.12) * 5.2;
        const yBtc = midY - evalCurve(curves[0], u, phase) * plotH;
        ctx.beginPath();
        ctx.arc(pointerX, yBtc, 4.2, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(214, 152, 92, 1)";
        ctx.fill();
        if (coordLabel) {
          const fmt = (v) => `${v >= 0 ? "+" : ""}${v.toFixed(2)}σ`;
          coordLabel.textContent = `BTC ${fmt(btcVal)} · ETH ${fmt(ethVal)}`;
        }
      }
    }

    function tick() {
      currentBoost += (focusBoost - currentBoost) * 0.08;
      phase += 0.0065 + currentBoost * 0.003;
      draw();
      rafId = requestAnimationFrame(tick);
    }

    canvas.addEventListener("pointermove", (e) => {
      const rect = canvas.getBoundingClientRect();
      pointerX = e.clientX - rect.left;
      pointerY = e.clientY - rect.top;
      if (reducedMotion) draw();
    });

    canvas.addEventListener("pointerleave", () => {
      pointerX = null;
      pointerY = null;
      if (coordLabel) coordLabel.textContent = "CLOSED-BAR ENGINE · D3";
      if (reducedMotion) draw();
    });

    const ro = new ResizeObserver(resize);
    ro.observe(canvas);
    resize();

    if (!reducedMotion) {
      rafId = requestAnimationFrame(tick);
    }

    document.addEventListener("visibilitychange", () => {
      if (reducedMotion) return;
      if (document.hidden && rafId) {
        cancelAnimationFrame(rafId);
        rafId = null;
      } else if (!document.hidden && !rafId) {
        rafId = requestAnimationFrame(tick);
      }
    });
  }

  loadStatus();
  initRegimeCanvas();
})();
