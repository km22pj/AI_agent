"use strict";
/*
 * Healthlingo front-end. Every number on screen comes from the server:
 * nothing is hard-coded, and missing information is shown as missing (never 0).
 */
(() => {
  // ------------------------------------------------------------------ helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value == null || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key === "style") el.style.cssText = value;
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else if (value === true) el.setAttribute(key, "");
      else el.setAttribute(key, value);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : String(kid));
    }
    return el;
  }
  const btn = (label, cls, onclick, extra = {}) => h("button", { type: "button", class: cls, onclick, ...extra }, label);
  const spinner = () => h("span", { class: "spinner", "aria-hidden": "true" });

  const MEAL_TYPES = [
    ["breakfast", "아침", "☀"],
    ["lunch", "점심", "◷"],
    ["dinner", "저녁", "☾"],
    ["snack", "간식", "♧"],
  ];
  const MEAL_LABEL = Object.fromEntries(MEAL_TYPES.map(([k, l]) => [k, l]));
  const MEAL_ICON = Object.fromEntries(MEAL_TYPES.map(([k, , i]) => [k, i]));
  const NUTRIENTS = [
    ["kcal", "열량", "kcal"],
    ["carbs_g", "탄수화물", "g"],
    ["protein_g", "단백질", "g"],
    ["fat_g", "지방", "g"],
  ];
  const EQUIPMENT = ["맨몸", "요가 매트", "덤벨", "밴드", "케틀벨", "헬스장", "걷기·달리기 공간", "자전거"];
  const CONFIDENCE = { high: "높음", medium: "보통", low: "낮음" };
  const STATUS_TEXT = {
    ai: "AI 추정",
    user: "직접 입력",
    none: "분석 전",
    pending: "분석 중…",
    failed: "분석 실패",
    stale: "재분석 필요",
  };

  // ------------------------------------------------------------------ state
  const state = {
    config: null,
    user: null,
    settings: null,
    epoch: 0, // bumps on every sign-in/out so late responses from a previous account are dropped
    overview: null,
    food: freshFood(),
    flow: freshFlow(),
    body: freshBody(),
    gallery: freshGallery(),
    workout: freshWorkout(),
    mealForm: null,
    bodyForm: null,
    analyzing: new Set(),
  };
  function freshWorkout() { return { data: null, token: 0, error: null, busy: false, requestError: null }; }
  function freshFood() { return { date: null, token: 0, day: null, stats: null, error: null, coachBusy: false, coachError: null }; }
  function freshFlow() { return { token: 0, status: "idle", prepared: null, clientId: null, photo: null, recognition: null, error: null }; }
  function freshBody() { return { month: null, selected: null, logsByDate: new Map(), token: 0, loaded: false, error: null }; }
  function freshGallery() { return { order: "desc", from: "", to: "", compare: false, picked: [], logs: null, token: 0, error: null }; }

  // ------------------------------------------------------------------ dates (user's time zone)
  function userTz() {
    const browser = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return (state.settings && state.settings.timezone) || browser || "UTC";
  }
  function todayStr() {
    try {
      return new Intl.DateTimeFormat("en-CA", { timeZone: userTz(), year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
    } catch {
      const d = new Date();
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    }
  }
  function nowTimeStr() {
    try {
      return new Intl.DateTimeFormat("en-GB", { timeZone: userTz(), hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date());
    } catch {
      const d = new Date();
      return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    }
  }
  const ymd = (s) => s.split("-").map(Number);
  const utcDate = (s) => { const [y, m, d] = ymd(s); return new Date(Date.UTC(y, m - 1, d)); };
  const addDays = (s, n) => { const d = utcDate(s); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const daysBetween = (a, b) => Math.round((utcDate(b) - utcDate(a)) / 86400000);
  const fmtDate = (s, opts = { month: "long", day: "numeric" }) => new Intl.DateTimeFormat("ko-KR", { ...opts, timeZone: "UTC" }).format(utcDate(s));
  const fmtDateLong = (s) => fmtDate(s, { year: "numeric", month: "long", day: "numeric", weekday: "short" });
  const weekdayShort = (s) => fmtDate(s, { weekday: "short" });
  function fmtStamp(iso) {
    try {
      return new Intl.DateTimeFormat("ko-KR", { timeZone: userTz(), month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(iso));
    } catch { return iso; }
  }
  const fmtNum = (v) => Math.round(v).toLocaleString("ko-KR");
  const fmt1 = (v) => Number(v).toLocaleString("ko-KR", { maximumFractionDigits: 1 }); // body values like 58.5kg

  function guessMealType(time) {
    if (!time) return "";
    const [hh, mm] = time.split(":").map(Number);
    const m = hh * 60 + mm;
    if (m < 630) return "breakfast";
    if (m < 900) return "lunch";
    if (m < 1020) return "snack";
    if (m < 1290) return "dinner";
    return "snack";
  }

  function newId() {
    if (crypto.randomUUID) return crypto.randomUUID().replace(/-/g, "");
    const a = new Uint8Array(16);
    crypto.getRandomValues(a);
    return [...a].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  // ------------------------------------------------------------------ API
  class ApiError extends Error {
    constructor(code, message, extra = {}) {
      super(message);
      this.code = code;
      Object.assign(this, extra);
    }
  }

  // AI requests may try a second model on the server; never leave the user waiting longer than this.
  const AI_TIMEOUT_MS = 120000;

  async function api(path, { method = "GET", body, timeoutMs } = {}) {
    let res;
    const controller = timeoutMs ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      res = await fetch(path, {
        method,
        credentials: "same-origin",
        headers: { "X-Healthlingo": "1", ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: controller ? controller.signal : undefined,
      });
    } catch {
      if (controller && controller.signal.aborted) {
        throw new ApiError("timeout", "AI 응답이 너무 오래 걸려 기다리지 않았어요. 잠시 후 다시 시도하거나 직접 입력해 주세요.", { retryable: true });
      }
      throw new ApiError("network", "서버에 연결하지 못했어요. 인터넷 연결이나 서버 실행 상태를 확인하고 다시 시도해 주세요.", { retryable: true });
    } finally {
      if (timer) clearTimeout(timer);
    }
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (!res.ok) {
      const err = (data && data.error) || {};
      const error = new ApiError(err.code || `http_${res.status}`, err.message || "요청을 처리하지 못했어요. 잠시 후 다시 시도해 주세요.", {
        status: res.status,
        retryable: err.retryable,
        data,
      });
      if (res.status === 401 && !path.startsWith("/api/auth") && state.user) handleUnauthorized();
      // The invite link was changed: reload so the server shows its "invite needed" page.
      if (res.status === 403 && error.code === "invite_required") window.location.reload();
      if (res.status === 403 && error.code === "not_invited" && state.user) handleUnauthorized(error.message);
      throw error;
    }
    return data;
  }

  // ------------------------------------------------------------------ UI primitives
  const toast = $("#toast");
  let toastTimer;
  function notify(message) {
    toast.textContent = message;
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 3200);
  }

  function openDialog(dialog) { if (!dialog.open) dialog.showModal(); }
  function closeDialog(dialog) { if (dialog.open) dialog.close(); }

  function confirmBox(message, okLabel = "확인") {
    const dialog = $("#confirm-dialog");
    $("#confirm-message").textContent = message;
    $("#confirm-ok").textContent = okLabel;
    openDialog(dialog);
    return new Promise((resolve) => {
      const finish = (value) => {
        $("#confirm-ok").onclick = null;
        $("#confirm-cancel").onclick = null;
        dialog.onclose = null;
        closeDialog(dialog);
        resolve(value);
      };
      $("#confirm-ok").onclick = () => finish(true);
      $("#confirm-cancel").onclick = () => finish(false);
      dialog.onclose = () => finish(false);
    });
  }

  // Disables a button while `work` runs, so double clicks cannot send twice.
  async function withBusy(button, busyLabel, work) {
    if (button && button.dataset.busy === "1") return undefined;
    const original = button ? button.textContent : "";
    if (button) {
      button.dataset.busy = "1";
      button.disabled = true;
      button.replaceChildren(spinner(), " ", busyLabel);
    }
    try {
      return await work();
    } finally {
      if (button) {
        button.dataset.busy = "";
        button.disabled = false;
        button.textContent = original;
      }
    }
  }

  function showFormError(id, message) {
    const el = $(id);
    el.textContent = message || "";
    el.hidden = !message;
  }

  function emptyState(title, text, ...actions) {
    return h("div", { class: "empty-state" }, h("strong", { text: title }), text ? h("span", { text }) : null, actions.length ? h("div", { class: "upload-actions" }, actions) : null);
  }
  const loadingLine = (text) => h("div", { class: "loading-line" }, spinner(), text);
  function errorNotice(error, retry) {
    return h("div", { class: "notice error" }, error.message, retry ? h("div", { class: "notice-actions" }, btn("다시 시도", "mini-button", retry)) : null);
  }
  function statusChip(status) {
    return h("span", { class: `status-chip ${status}` }, status === "pending" ? spinner() : null, STATUS_TEXT[status] || status);
  }

  // ------------------------------------------------------------------ images
  function loadImageElement(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("이 사진 형식은 열 수 없어요. JPG나 PNG 사진을 선택해 주세요.")); };
      img.src = url;
    });
  }
  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("사진을 읽지 못했어요."));
      reader.readAsDataURL(blob);
    });
  }
  async function toJpeg(source, maxSide, quality) {
    const width = source.width || source.naturalWidth;
    const height = source.height || source.naturalHeight;
    const scale = Math.min(1, maxSide / Math.max(width, height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
    if (!blob) throw new Error("사진을 변환하지 못했어요.");
    return { blob, dataUrl: await blobToDataUrl(blob) };
  }
  // Re-encodes the photo (drops location metadata) and makes a thumbnail.
  async function prepareImage(file) {
    if (!file || !(file.type || "").startsWith("image/")) throw new Error("이미지 파일을 선택해 주세요.");
    if (file.size > 30 * 1024 * 1024) throw new Error("사진 용량이 너무 커요. 30MB 이하의 사진을 선택해 주세요.");
    let source;
    try {
      source = await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      source = await loadImageElement(file);
    }
    const full = await toJpeg(source, 1600, 0.85);
    const thumb = await toJpeg(source, 400, 0.8);
    if (source.close) source.close();
    return { image: full.dataUrl, thumb: thumb.dataUrl, previewUrl: URL.createObjectURL(full.blob) };
  }
  async function uploadPhoto(kind, prepared, clientId) {
    const res = await api("/api/photos", { method: "POST", body: { client_id: clientId, kind, image: prepared.image, thumb: prepared.thumb } });
    return res.photo;
  }

  // ------------------------------------------------------------------ auth
  let gisLoading = null;
  function loadGoogleScript() {
    if (window.google && window.google.accounts && window.google.accounts.id) return Promise.resolve();
    if (!gisLoading) {
      gisLoading = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "https://accounts.google.com/gsi/client";
        script.async = true;
        script.onload = () => resolve();
        script.onerror = () => { gisLoading = null; reject(new Error("Google 로그인 스크립트를 불러오지 못했어요. 인터넷 연결을 확인해 주세요.")); };
        document.head.append(script);
      });
    }
    return gisLoading;
  }

  function authStatus(message, isError = false) {
    const el = $("#auth-status");
    el.textContent = message;
    el.classList.toggle("error", isError);
  }

  async function showSignedOut(message) {
    document.body.classList.add("signed-out");
    const cfg = state.config;
    $("#dev-login").hidden = !(cfg && cfg.dev_login_enabled);
    const slot = $("#google-signin");
    slot.replaceChildren();
    slot.hidden = !(cfg && cfg.google_configured);
    if (!cfg) return;
    if (!cfg.google_configured) {
      authStatus(
        (message ? message + " " : "") +
          "Google 로그인이 아직 설정되지 않았어요. 서버에 GOOGLE_CLIENT_ID를 설정해야 로그인할 수 있어요. (README의 ‘Google 로그인 설정’ 참고)",
        true,
      );
      return;
    }
    if (EMBEDDED) {
      authStatus((message ? message + " " : "") + "이 화면이 다른 창 안에 열려 있어 로그인이 유지되지 않을 수 있어요. Chrome이나 Edge 주소창에 http://localhost:8000 을 직접 열어 주세요.", true);
    } else {
      authStatus(message || "Google 계정으로 로그인해 주세요.", false);
    }
    try {
      await loadGoogleScript();
      window.google.accounts.id.initialize({
        client_id: cfg.google_client_id,
        callback: onGoogleCredential,
        auto_select: false,
        ux_mode: "popup",
        cancel_on_tap_outside: true,
      });
      window.google.accounts.id.renderButton(slot, { theme: "outline", size: "large", text: "signin_with", shape: "pill", locale: "ko", width: 260 });
    } catch (error) {
      authStatus(error.message, true);
    }
  }

  const EMBEDDED = (() => { try { return window.top !== window.self; } catch { return true; } })();
  const COOKIE_BLOCKED =
    "Google 계정 확인은 성공했지만, 브라우저가 로그인 정보(쿠키)를 저장하지 않아 로그인을 유지할 수 없어요. " +
    (EMBEDDED
      ? "지금 화면이 VS Code 내장 브라우저처럼 다른 화면 안에 열려 있어요. Chrome이나 Edge 주소창에 http://localhost:8000 을 직접 열어 주세요."
      : "브라우저에서 localhost의 쿠키가 차단돼 있는지 확인해 주세요.");

  // The server answered "signed in"; make sure the browser actually kept the session cookie.
  async function completeSignIn() {
    try {
      const me = await api("/api/me");
      await onSignedIn(me);
    } catch (error) {
      authStatus(error.status === 401 ? COOKIE_BLOCKED : error.message, true);
    }
  }

  async function onGoogleCredential(response) {
    authStatus("로그인 정보를 확인하는 중이에요…");
    try {
      await api("/api/auth/google", { method: "POST", body: { credential: response && response.credential } });
    } catch (error) {
      authStatus(error.message, true);
      return;
    }
    await completeSignIn();
  }

  $("#dev-login").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    await withBusy(button, "확인 중", async () => {
      try {
        await api("/api/auth/dev", { method: "POST", body: { name: $("#dev-login-name").value } });
      } catch (error) {
        authStatus(error.message, true);
        return;
      }
      await completeSignIn();
    });
  });

  async function onSignedIn(data) {
    resetUserState();
    state.epoch += 1;
    state.user = data.user;
    state.settings = data.settings;
    document.body.classList.remove("signed-out");
    if (!state.settings.timezone) {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (tz) {
        api("/api/settings", { method: "PUT", body: { timezone: tz } })
          .then((res) => { if (state.user && state.user.id === data.user.id) { state.settings = res.settings; renderProfile(); } })
          .catch(() => {});
        state.settings = { ...state.settings, timezone: tz };
      }
    }
    renderChrome();
    renderProfile();
    showScreen("today");
  }

  async function logout() {
    try { await api("/api/auth/logout", { method: "POST", body: {} }); } catch { /* local sign-out still happens */ }
    if (window.google && window.google.accounts && window.google.accounts.id) window.google.accounts.id.disableAutoSelect();
    resetUserState();
    state.user = null;
    state.settings = null;
    state.epoch += 1;
    showSignedOut("로그아웃했어요. 다시 로그인하면 내 기록을 불러와요.");
  }

  function handleUnauthorized(message) {
    resetUserState();
    state.user = null;
    state.settings = null;
    state.epoch += 1;
    showSignedOut(message || "로그인이 만료됐어요. 다시 로그인해 주세요.");
  }

  // Wipes everything the previous account put on screen.
  function resetUserState() {
    $$("dialog").forEach(closeDialog);
    if (state.flow.prepared) URL.revokeObjectURL(state.flow.prepared.previewUrl);
    if (state.bodyForm && state.bodyForm.prepared) URL.revokeObjectURL(state.bodyForm.prepared.previewUrl);
    state.overview = null;
    state.food = freshFood();
    state.flow = freshFlow();
    state.body = freshBody();
    state.gallery = freshGallery();
    state.workout = freshWorkout();
    state.mealForm = null;
    state.bodyForm = null;
    state.analyzing = new Set();
    ["#meal-list", "#nutrition-body", "#trend-body", "#coach-body", "#food-flow", "#calendar-grid", "#body-day", "#settings-summary", "#week-days", "#view-dialog-body", "#view-dialog-foot", "#routine-body"].forEach((sel) => $(sel).replaceChildren());
    ["#meal-count", "#meal-analyzed-count", "#meal-pending-count", "#stat-movement", "#stat-meals", "#stat-body", "#stat-days", "#streak-days"].forEach((sel) => { $(sel).textContent = "–"; });
    $("#streak-copy").textContent = "기록을 불러오는 중이에요…";
    $("#sidebar-name").textContent = "나의 프로필";
    $("#sidebar-email").textContent = "로그인 필요";
    $("#sidebar-avatar").replaceChildren("🌼");
    $("#profile-avatar").replaceChildren("🌼");
    $("#profile-name").textContent = "나의 프로필";
    $("#profile-goal").textContent = "";
    $("#welcome-name").textContent = "";
    $("#week-summary-text").textContent = "기록을 불러오는 중이에요…";
    $("#compare-note").textContent = "기록을 불러오는 중이에요…";
    $("#day-complete").checked = false;
    resetComparePreview();
    renderMissions(null);
  }

  // ------------------------------------------------------------------ navigation
  function showScreen(name) {
    $$(".screen").forEach((screen) => screen.classList.toggle("active", screen.id === `screen-${name}`));
    $$(".nav-button[data-screen]").forEach((button) => {
      if (button.dataset.screen === name) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    const heading = $(`#screen-${name} h1`);
    if (heading) document.title = `${heading.textContent} — Healthlingo`;
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (!state.user) return;
    if (name === "today" || name === "profile") loadOverview();
    if (name === "food") {
      if (!state.food.date) state.food.date = todayStr();
      loadFoodDay();
    }
    if (name === "body") {
      if (!state.body.month) state.body.month = todayStr().slice(0, 8) + "01";
      loadBodyMonth();
      loadComparePreview();
      loadWorkout();
    }
  }

  function renderChrome() {
    const today = todayStr();
    $("#header-date").textContent = fmtDate(today, { month: "long", day: "numeric", weekday: "long" });
    $("#welcome-date").textContent = new Intl.DateTimeFormat("en-US", { weekday: "long", month: "long", day: "numeric", timeZone: "UTC" }).format(utcDate(today)).toUpperCase();
    $("#today-date-button").textContent = `오늘 · ${fmtDate(today)}`;
    const user = state.user;
    $("#sidebar-name").textContent = user.name || "나의 프로필";
    $("#sidebar-email").textContent = user.is_dev ? "개발용 테스트 계정" : user.email || "Google 계정";
    $("#welcome-name").textContent = user.name ? `${user.name.replace(/ \(개발용\)$/, "")}님, ` : "";
    for (const sel of ["#sidebar-avatar", "#profile-avatar"]) {
      const slot = $(sel);
      slot.replaceChildren(user.picture ? h("img", { src: user.picture, alt: "", referrerpolicy: "no-referrer" }) : "🌼");
    }
    const ai = state.config.ai_configured;
    $("#ai-status-tag").textContent = ai ? "AI 참고 피드백" : "AI 미설정 · 직접 입력 가능";
    $("#food-ai-line").replaceChildren(
      h("span", { "aria-hidden": "true" }, "ⓘ"),
      h("span", { text: ai ? "열량·탄수화물·단백질·지방은 모두 AI 추정치예요. 사진과 식사 정보는 분석할 때만 AI 서비스로 전송돼요." : "AI 분석이 설정되지 않았어요. 사진은 첨부만 되고, 음식 정보는 직접 입력해 저장할 수 있어요." }),
    );
    $("#footnote").textContent = `기록은 로그인한 내 계정에만 저장돼요 · AI 분석: ${ai ? "연결됨" : "미설정"}`;
  }

  // ------------------------------------------------------------------ today & profile
  async function loadOverview() {
    const epoch = state.epoch;
    try {
      const overview = await api(`/api/overview?today=${todayStr()}`);
      if (epoch !== state.epoch) return;
      state.overview = overview;
      renderOverview();
    } catch (error) {
      if (epoch !== state.epoch) return;
      $("#week-summary-text").textContent = error.message;
    }
  }

  function renderOverview() {
    const ov = state.overview;
    if (!ov) return;
    const today = ov.today;
    $("#week-range").textContent = `${fmtDate(ov.week[0].date)} — ${fmtDate(ov.week[6].date)}`;
    $("#week-days").replaceChildren(
      ...ov.week.map((day) => {
        const isToday = day.date === today;
        const cls = ["week-day", day.has_record ? "done" : "", isToday ? "today" : ""].join(" ");
        const label = `${fmtDate(day.date, { month: "long", day: "numeric", weekday: "long" })} ${day.has_record ? "기록 있음" : "기록 없음"}`;
        return h("div", { class: cls }, h("span", { text: weekdayShort(day.date) }), h("span", { class: "week-dot", "aria-label": label }, day.has_record ? "✓" : isToday ? "•" : "·"));
      }),
    );
    $("#week-summary-text").textContent = ov.week_record_days
      ? `이번 주에 ${ov.week_record_days}일 나를 위한 기록을 남겼어요.`
      : "이번 주 기록이 아직 없어요. 오늘 하나 남겨 볼까요?";
    renderStreak(ov.streak);
    renderMissions(ov.missions);

    $("#stat-movement").textContent = `${ov.month.movement}회`;
    $("#stat-meals").textContent = `${ov.month.meals}회`;
    $("#stat-body").textContent = `${ov.month.body_logs}회`;
    $("#stat-days").textContent = `${ov.month.record_days}일`;
    $$(".badge-card[data-badge]").forEach((card) => {
      const earned = Boolean(ov.badges[card.dataset.badge]);
      card.classList.toggle("locked", !earned);
      card.setAttribute("aria-label", `${card.querySelector("strong").textContent} ${earned ? "획득" : "아직 잠김"}`);
    });
  }

  function renderStreak(streak) {
    if (!streak) return;
    const { current, best, today_recorded: todayDone } = streak;
    $("#streak-days").textContent = String(current);
    $("#streak-title").textContent = current
      ? todayDone ? `오늘까지 ${current}일 연속으로 기록했어요` : `어제까지 ${current}일 연속으로 기록했어요`
      : "연속 기록";
    const bestText = best > current ? ` 지금까지 가장 길게 이어간 기록은 ${best}일이에요.` : "";
    $("#streak-copy").textContent = current
      ? (todayDone ? "식사·바디 로그·오늘의 목표 중 하나라도 남긴 날이 이어지면 늘어나요." :`오늘 기록하면 ${current + 1}일째가 돼요.`) + bestText
      : (best ? `오늘 기록하면 새 연속 기록이 시작돼요.${bestText}` : "오늘 기록하면 연속 기록이 시작돼요.");
  }

  function renderMissions(missions) {
    const done = missions || { warmup: false, movement: false, record: false };
    let count = 0;
    $$(".mission[data-mission]").forEach((mission) => {
      const key = mission.dataset.mission;
      const isDone = Boolean(done[key]);
      if (isDone) count += 1;
      mission.classList.toggle("is-done", isDone);
      const button = mission.querySelector(".mission-action");
      button.textContent = isDone ? "완료 ✓" : key === "record" ? "기록하기" : "완료하기";
      button.disabled = !missions;
    });
    $("#completed-count").textContent = String(count);
    $("#ring-count").textContent = `${count}/3`;
    $(".progress-fill").style.width = `${(count / 3) * 100}%`;
    $(".progress-track").setAttribute("aria-valuenow", String(count));
    const ring = $("#today-ring");
    ring.style.setProperty("--progress", `${(count / 3) * 100}%`);
    ring.setAttribute("aria-label", `오늘 목표 3개 중 ${count}개 완료`);
    $("#rhythm-title").textContent = count === 3 ? "오늘 목표를 모두 채웠어요" : count ? "나만의 속도로 좋아요" : "가볍게 시작해 봐요";
    $("#rhythm-copy").textContent = count ? "오늘의 작은 목표가 차곡차곡 쌓이고 있어요." : "오늘의 작은 목표를 하나씩 채워 봐요.";
    const s = state.settings;
    const minutes = (s && s.minutes_per_session) || 15;
    const equipment = s && s.equipment && s.equipment.length ? `${s.equipment.slice(0, 2).join("·")} 활용` : "편안한 강도";
    $("#movement-detail").textContent = `${minutes}분 · ${equipment}`;
  }

  $$(".mission[data-mission] .mission-action").forEach((button) => {
    button.addEventListener("click", async () => {
      const key = button.closest(".mission").dataset.mission;
      const ov = state.overview;
      if (!ov) return;
      if (key === "record") {
        if (ov.missions.record) notify("오늘 남긴 기록이 있어요. 식사 화면에서 확인할 수 있어요.");
        goToFood(todayStr(), !ov.missions.record);
        return;
      }
      const done = ov.missions[key];
      const epoch = state.epoch;
      await withBusy(button, "저장 중", async () => {
        try {
          if (done) await api(`/api/activities?date=${ov.today}&kind=${key}`, { method: "DELETE" });
          else await api("/api/activities", { method: "POST", body: { date: ov.today, kind: key } });
          if (epoch !== state.epoch) return;
          notify(done ? "완료 표시를 취소했어요." : key === "warmup" ? "몸 풀기를 기록했어요. 좋은 시작이에요!" : "나에게 맞는 움직임을 기록했어요. 잘했어요!");
        } catch (error) {
          notify(error.message);
        }
      });
      if (epoch === state.epoch) loadOverview();
    });
  });

  function settingsAreEmpty(s) {
    if (!s) return true;
    const targets = s.targets || {};
    const body = s.body || {};
    return !s.goal_type && !s.experience && !(s.allergies || []).length && !(s.avoid_foods || []).length && !Object.values(targets).some((v) => v != null) && !s.food_preferences && body.height_cm == null && body.weight_kg == null;
  }

  function renderProfile() {
    const user = state.user;
    const s = state.settings;
    if (!user || !s) return;
    $("#profile-name").textContent = user.name || "나의 프로필";
    const goal = [s.goal_type, s.goal_note].filter(Boolean).join(" · ");
    $("#profile-goal").textContent = goal ? `나의 목표 · ${goal}` : "목표가 아직 없어요. ‘목표 설정’에서 정할 수 있어요.";
    $("#profile-account-tag").textContent = user.is_dev ? "개발용 테스트 계정" : "Google 계정";
    $("#notify-toggle").setAttribute("aria-checked", String(Boolean(s.notifications)));
    $("#setup-banner").hidden = !settingsAreEmpty(s);
    const t = s.targets || {};
    const targetText = NUTRIENTS.filter(([k]) => t[k] != null).map(([k, label, unit]) => `${label} ${fmtNum(t[k])}${unit}`).join(" · ");
    const rows = [
      ["목표", goal || "설정 안 함"],
      ["현재 몸 상태", bodyText(s.body) || "설정 안 함"],
      ["운동 경험", s.experience || "설정 안 함"],
      ["사용 가능한 장비", [...(s.equipment || []), s.equipment_note].filter(Boolean).join(", ") || "설정 안 함"],
      ["운동 계획", s.workouts_per_week != null || s.minutes_per_session != null ? `주 ${s.workouts_per_week ?? "?"}회 · ${s.minutes_per_session ?? "?"}분` : "설정 안 함"],
      ["식사 선호", s.food_preferences || "설정 안 함"],
      ["알레르기", (s.allergies || []).join(", ") || "없음"],
      ["피하고 싶은 음식", (s.avoid_foods || []).join(", ") || "없음"],
      ["하루 영양 목표", targetText || "설정 안 함 (달성률 미표시)"],
      ["시간대", userTz()],
    ];
    $("#settings-summary").replaceChildren(...rows.map(([label, value]) => h("div", null, h("span", { text: label }), value)));
    renderMissions(state.overview ? state.overview.missions : null);
  }

  function bodyText(body) {
    const b = body || {};
    return [
      b.height_cm != null ? `키 ${fmt1(b.height_cm)}cm` : null,
      b.weight_kg != null ? `몸무게 ${fmt1(b.weight_kg)}kg` : null,
      b.skeletal_muscle_kg != null ? `골격근량 ${fmt1(b.skeletal_muscle_kg)}kg` : null,
    ].filter(Boolean).join(" · ");
  }

  // ------------------------------------------------------------------ settings form
  function openSettings({ focus = null } = {}) {
    const s = state.settings;
    if (!s) return;
    const body = s.body || {};
    $("#set-height").value = body.height_cm ?? "";
    $("#set-weight").value = body.weight_kg ?? "";
    $("#set-muscle").value = body.skeletal_muscle_kg ?? "";
    $("#set-condition").value = body.condition_note || "";
    $("#set-goal").value = s.goal_type || "";
    $("#set-experience").value = s.experience || "";
    $("#set-goal-note").value = s.goal_note || "";
    $("#set-equipment").replaceChildren(
      ...EQUIPMENT.map((name) => h("label", { class: "chip-option" }, h("input", { type: "checkbox", value: name, checked: (s.equipment || []).includes(name) }), h("span", { text: name }))),
    );
    $$("#set-equipment input").forEach((input) => { input.checked = (s.equipment || []).includes(input.value); });
    $("#set-equipment-note").value = s.equipment_note || "";
    $("#set-per-week").value = s.workouts_per_week ?? "";
    $("#set-minutes").value = s.minutes_per_session ?? "";
    $("#set-pref").value = s.food_preferences || "";
    $("#set-allergies").value = (s.allergies || []).join(", ");
    $("#set-avoid").value = (s.avoid_foods || []).join(", ");
    const t = s.targets || {};
    $("#set-kcal").value = t.kcal ?? "";
    $("#set-carbs").value = t.carbs_g ?? "";
    $("#set-protein").value = t.protein_g ?? "";
    $("#set-fat").value = t.fat_g ?? "";
    $("#set-timezone").textContent = `기록 날짜는 ${userTz()} 시간대 기준으로 계산해요.` + (s.targets_confirmed_at ? ` · 영양 목표 확인: ${fmtStamp(s.targets_confirmed_at)}` : "");
    showFormError("#settings-form-error", "");
    openDialog($("#settings-dialog"));
    if (focus) {
      $("#set-body-section").scrollIntoView({ block: "start" });
      $(focus).focus({ preventScroll: true });
    }
  }

  const numOrNull = (sel) => { const v = $(sel).value.trim(); return v === "" ? null : Number(v); };

  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const epoch = state.epoch;
    const payload = {
      goal_type: $("#set-goal").value,
      goal_note: $("#set-goal-note").value,
      experience: $("#set-experience").value,
      equipment: $$("#set-equipment input:checked").map((i) => i.value),
      equipment_note: $("#set-equipment-note").value,
      workouts_per_week: numOrNull("#set-per-week"),
      minutes_per_session: numOrNull("#set-minutes"),
      food_preferences: $("#set-pref").value,
      allergies: $("#set-allergies").value,
      avoid_foods: $("#set-avoid").value,
      targets: { kcal: numOrNull("#set-kcal"), carbs_g: numOrNull("#set-carbs"), protein_g: numOrNull("#set-protein"), fat_g: numOrNull("#set-fat") },
      body: { height_cm: numOrNull("#set-height"), weight_kg: numOrNull("#set-weight"), skeletal_muscle_kg: numOrNull("#set-muscle"), condition_note: $("#set-condition").value },
    };
    await withBusy($("#settings-save"), "저장 중", async () => {
      try {
        const res = await api("/api/settings", { method: "PUT", body: payload });
        if (epoch !== state.epoch) return;
        state.settings = res.settings;
        closeDialog($("#settings-dialog"));
        renderProfile();
        notify("개인 설정을 저장했어요.");
        if (state.food.date && $("#screen-food").classList.contains("active")) loadFoodDay({ keep: true });
        if ($("#screen-body").classList.contains("active")) loadWorkout();
      } catch (error) {
        showFormError("#settings-form-error", error.message);
      }
    });
  });

  $("#notify-toggle").addEventListener("click", async (event) => {
    const toggle = event.currentTarget;
    const next = toggle.getAttribute("aria-checked") !== "true";
    toggle.setAttribute("aria-checked", String(next));
    try {
      const res = await api("/api/settings", { method: "PUT", body: { notifications: next } });
      state.settings = res.settings;
      notify(next ? "부드러운 알림 설정을 켰어요. (알림 발송 기능은 아직 없어요)" : "알림 설정을 껐어요.");
    } catch (error) {
      toggle.setAttribute("aria-checked", String(!next));
      notify(error.message);
    }
  });

  // ------------------------------------------------------------------ food: day view
  function goToFood(date, openForm = false) {
    state.food.date = date;
    showScreen("food");
    if (openForm) openMealForm({ date });
  }

  async function loadFoodDay({ keep = false } = {}) {
    const food = state.food;
    const epoch = state.epoch;
    const token = ++food.token;
    const date = food.date;
    if (!keep) {
      // Clear the previous date's numbers right away so they are never shown for this date.
      food.day = null;
      food.stats = null;
      food.coachError = null;
    }
    food.error = null;
    renderFood();
    try {
      const [day, stats] = await Promise.all([
        api(`/api/days/${date}`),
        api(`/api/stats/range?from=${addDays(date, -6)}&to=${date}`),
      ]);
      if (epoch !== state.epoch || token !== food.token) return;
      food.day = day;
      food.stats = stats;
    } catch (error) {
      if (epoch !== state.epoch || token !== food.token) return;
      food.error = error;
    }
    renderFood();
  }

  function setFoodDate(date) {
    if (!date) return;
    const today = todayStr();
    state.food.date = date > today ? today : date;
    state.food.coachError = null;
    loadFoodDay();
  }

  function renderFood() {
    const food = state.food;
    const today = todayStr();
    const date = food.date;
    $("#food-date").value = date;
    $("#food-date").max = today;
    $("#food-next-day").disabled = date >= today;
    $("#meal-day-title").textContent = date === today ? "오늘의 식사" : `${fmtDate(date, { month: "long", day: "numeric", weekday: "short" })} 식사`;
    $("#nutrition-title").textContent = `${fmtDate(date)} 영양 합계`;
    renderFlow();

    if (food.error && !food.day) {
      const retry = () => loadFoodDay();
      $("#meal-list").replaceChildren(errorNotice(food.error, retry));
      $("#nutrition-body").replaceChildren(h("p", { class: "hint", text: "기록을 불러오지 못해 합계를 표시할 수 없어요." }));
      $("#trend-body").replaceChildren();
      $("#coach-body").replaceChildren();
      ["#meal-count", "#meal-analyzed-count", "#meal-pending-count"].forEach((sel) => { $(sel).textContent = "–"; });
      return;
    }
    if (!food.day) {
      $("#meal-list").replaceChildren(loadingLine("기록을 불러오는 중이에요…"));
      $("#nutrition-body").replaceChildren(loadingLine("계산 중이에요…"));
      $("#trend-body").replaceChildren(loadingLine("불러오는 중이에요…"));
      $("#coach-body").replaceChildren();
      ["#meal-count", "#meal-analyzed-count", "#meal-pending-count"].forEach((sel) => { $(sel).textContent = "–"; });
      return;
    }
    if (food.error) notify(food.error.message);
    renderMealList();
    renderNutrition();
    renderTrend();
    renderCoach();
  }

  function mealTitle(meal) {
    return meal.items.map((i) => i.name).join(" · ");
  }

  function renderMealList() {
    const { day } = state.food;
    const meals = day.meals;
    const counted = meals.filter((m) => m.nutrition_status === "ai" || m.nutrition_status === "user").length;
    $("#meal-count").textContent = `${meals.length}개`;
    $("#meal-analyzed-count").textContent = `${counted}개`;
    $("#meal-pending-count").textContent = `${meals.length - counted}개`;
    $("#day-complete").checked = Boolean(day.complete);
    if (!meals.length) {
      $("#meal-list").replaceChildren(
        emptyState(
          "아직 기록이 없어요",
          `${fmtDate(day.date)}에 먹은 식사를 남겨 보세요.`,
          btn("기록 추가", "primary-button small-button", () => openMealForm({ date: day.date })),
          btn("사진으로 추가", "quiet-button small-button", () => $("#food-photo-input").click()),
        ),
      );
      return;
    }
    const groups = MEAL_TYPES.map(([type, label]) => {
      const list = meals.filter((m) => m.meal_type === type);
      return h(
        "div",
        { class: "meal-group" },
        h("div", { class: "meal-group-head" }, h("span", { text: `${label} ${list.length ? `· ${list.length}개` : ""}` }), btn("＋ 추가", "mini-button", () => openMealForm({ date: day.date, mealType: type }))),
        list.length ? list.map(renderMealEntry) : h("p", { class: "meal-group-empty", text: "기록 없음" }),
      );
    });
    $("#meal-list").replaceChildren(...groups);
  }

  function renderMealEntry(meal) {
    const status = state.analyzing.has(meal.id) ? "pending" : meal.nutrition_status;
    const kcal = meal.nutrition && meal.nutrition.total.kcal;
    const amounts = meal.items.map((i) => i.amount).filter(Boolean).join(", ");
    const canAnalyze = state.config.ai_configured && ["none", "failed", "stale"].includes(status);
    const thumb = h(
      "button",
      { type: "button", class: "meal-thumb", "aria-label": `${mealTitle(meal)} 상세 보기`, onclick: () => openMealDetail(meal.id), style: meal.photo ? "" : "background:#fff1e8" },
      meal.photo ? h("img", { src: meal.photo.thumb_url, alt: "", loading: "lazy" }) : MEAL_ICON[meal.meal_type],
    );
    return h(
      "div",
      { class: "meal-entry", dataset: { mealId: meal.id } },
      thumb,
      h(
        "span",
        { class: "entry-main" },
        h("span", { class: "entry-title", text: mealTitle(meal) }),
        h("span", { text: [meal.time, amounts].filter(Boolean).join(" · ") || "시간·양 미입력" }),
        h("span", { style: "display:flex;flex-wrap:wrap;gap:4px;margin-top:5px" }, statusChip(status), kcal != null ? h("span", { class: "status-chip", text: `약 ${fmtNum(kcal)}kcal` }) : null),
        status === "failed" && meal.analysis_error ? h("span", { class: "danger-text", style: "color:#a14b36", text: meal.analysis_error }) : null,
        h(
          "span",
          { class: "entry-actions" },
          btn("상세", "mini-button", () => openMealDetail(meal.id)),
          btn("수정", "mini-button", () => openMealForm({ meal })),
          canAnalyze ? btn(status === "none" ? "AI 분석" : "다시 분석", "mini-button accent", () => analyzeMeal(meal.id)) : null,
          btn("삭제", "mini-button danger", () => deleteMeal(meal)),
        ),
      ),
    );
  }

  function findMeal(id) {
    return state.food.day && state.food.day.meals.find((m) => m.id === id);
  }

  async function analyzeMeal(id) {
    if (state.analyzing.has(id)) return;
    const epoch = state.epoch;
    state.analyzing.add(id);
    if (state.food.day) renderMealList();
    try {
      await api(`/api/meals/${id}/analyze`, { method: "POST", body: {}, timeoutMs: AI_TIMEOUT_MS });
      if (epoch !== state.epoch) return;
      notify("영양 분석을 마쳤어요. 그래프와 합계에 반영했어요.");
    } catch (error) {
      if (epoch !== state.epoch) return;
      notify(error.retryable ? `${error.message} (‘다시 분석’으로 재시도할 수 있어요)` : error.message);
    } finally {
      state.analyzing.delete(id);
    }
    if (epoch === state.epoch) {
      await loadFoodDay({ keep: true });
      if ($("#view-dialog").open && $("#view-dialog").dataset.mealId === id) openMealDetail(id);
    }
  }

  async function deleteMeal(meal) {
    const ok = await confirmBox(`‘${mealTitle(meal)}’ 기록을 삭제할까요? 사진과 분석 결과도 함께 삭제돼요.`, "삭제");
    if (!ok) return;
    const epoch = state.epoch;
    try {
      await api(`/api/meals/${meal.id}`, { method: "DELETE" });
      if (epoch !== state.epoch) return;
      closeDialog($("#view-dialog"));
      notify("식사 기록을 삭제했어요.");
      loadFoodDay({ keep: true });
      loadOverview();
    } catch (error) {
      notify(error.message);
    }
  }

  function renderNutrition() {
    const { day } = state.food;
    const summary = day.summary;
    const body = $("#nutrition-body");
    $("#nutrition-subtitle").textContent = `${fmtDateLong(day.date)} · 저장된 기록 ${summary.meal_count}개 기준`;
    if (!summary.meal_count) {
      body.replaceChildren(emptyState("아직 기록이 없어요", "식사를 기록하고 분석하면 합계가 표시돼요.", btn("기록 추가", "primary-button small-button", () => openMealForm({ date: day.date }))));
      return;
    }
    if (!summary.counted) {
      body.replaceChildren(
        h("div", { class: "notice info" }, h("strong", { text: "분석 전 · " }), `이 날짜의 기록 ${summary.meal_count}개가 아직 분석되지 않아 합계를 계산할 수 없어요. 기록의 ‘AI 분석’ 버튼이나 영양 정보 직접 입력을 이용해 주세요.`),
      );
      return;
    }
    const rows = NUTRIENTS.map(([key, label, unit]) => {
      const p = summary.progress[key];
      const total = summary.totals[key];
      const valueText = p.value == null ? "정보 부족" : `${fmtNum(p.value)}${unit}`;
      const right = p.target != null && p.value != null ? ` / 목표 ${fmtNum(p.target)}${unit} · ${p.pct}%` : p.target == null ? " · 목표 미설정" : "";
      return h(
        "div",
        { class: "nutrient-row" },
        h("div", { class: "nutrient-head" }, h("span", { text: label }), h("span", null, h("span", { class: "nutrient-value", text: valueText }), right)),
        p.target != null && p.value != null
          ? h("div", { class: "nutrient-track", role: "progressbar", "aria-label": `${label} 목표 대비`, "aria-valuenow": String(p.pct), "aria-valuemin": "0", "aria-valuemax": "100" }, h("div", { class: "nutrient-fill", style: `width:${Math.min(100, p.pct)}%` }))
          : null,
        total.missing ? h("p", { class: "hint", style: "margin-top:3px", text: `${total.missing}개 기록은 이 항목 정보가 없어 빠졌어요.` }) : null,
      );
    });
    const kids = [h("div", { class: "nutrient-list", style: "margin-top:0" }, rows)];
    const c = summary.totals.carbs_g.value, pr = summary.totals.protein_g.value, f = summary.totals.fat_g.value;
    if (c != null && pr != null && f != null && c * 4 + pr * 4 + f * 9 > 0) {
      const energy = c * 4 + pr * 4 + f * 9;
      const parts = [["탄수화물", (c * 4) / energy, "#7885d7"], ["단백질", (pr * 4) / energy, "#e9a08c"], ["지방", (f * 9) / energy, "#9eb9a3"]];
      kids.push(
        h("p", { class: "sub-title", text: "열량 구성 비율" }),
        h("div", { class: "macro-split", role: "img", "aria-label": parts.map(([n, v]) => `${n} ${Math.round(v * 100)}%`).join(", ") }, parts.map(([, v, color]) => h("span", { style: `width:${v * 100}%;background:${color}` }))),
        h("div", { class: "legend" }, parts.map(([n, v, color]) => h("span", null, h("i", { style: `background:${color}` }), `${n} ${Math.round(v * 100)}%`))),
      );
    }
    const ai = day.meals.filter((m) => m.nutrition_status === "ai").length;
    const user = day.meals.filter((m) => m.nutrition_status === "user").length;
    kids.push(h("p", { class: "hint", style: "margin-top:10px", text: `AI 추정 ${ai}끼 · 직접 입력 ${user}끼 기준 추정치예요.` }));
    if (summary.incomplete_data) {
      kids.push(h("div", { class: "notice" }, `분석되지 않았거나 일부 정보가 없는 기록이 있어 합계가 불완전해요. (합계 제외 ${summary.uncounted}개)`));
    }
    if (!summary.has_targets) {
      kids.push(h("div", { class: "notice info" }, "영양 목표가 설정되지 않아 달성률은 표시하지 않아요.", h("div", { class: "notice-actions" }, btn("영양 목표 설정", "mini-button", openSettings))));
    }
    body.replaceChildren(...kids);
  }

  function renderTrend() {
    const { stats, date } = state.food;
    const body = $("#trend-body");
    if (!stats) { body.replaceChildren(); return; }
    const days = stats.days;
    $("#trend-subtitle").textContent = `${fmtDate(days[0].date)} — ${fmtDate(days[6].date)} · 막대를 누르면 그 날짜로 이동해요`;
    if (!days.some((d) => d.meal_count)) {
      body.replaceChildren(emptyState("최근 7일 동안 기록이 없어요", "식사를 기록하면 날짜별 열량이 여기에 쌓여요."));
      return;
    }
    const target = stats.targets && stats.targets.kcal;
    const max = Math.max(target || 0, ...days.map((d) => d.totals.kcal || 0)) || 1;
    const chart = h(
      "div",
      { class: "week-chart", role: "list", "aria-label": "최근 7일 열량" },
      days.map((d) => {
        const value = d.totals.kcal;
        const partial = d.meal_count > 0 && d.uncounted > 0;
        const label = d.meal_count === 0 ? "기록 없음" : value == null ? "분석 전" : `${fmtNum(value)}kcal${partial ? " (불완전)" : ""}`;
        return h(
          "button",
          { type: "button", role: "listitem", class: `week-bar${d.date === date ? " selected" : ""}${partial ? " partial" : ""}`, "aria-label": `${fmtDate(d.date)} ${label}`, onclick: () => setFoodDate(d.date) },
          h("span", { class: "bar-area" }, value != null ? h("span", { class: "bar", style: `height:${Math.max(2, (value / max) * 100)}%` }) : null),
          h("span", { class: "bar-value", text: d.meal_count === 0 ? "–" : value == null ? "분석 전" : fmtNum(value) }),
          h("span", { text: `${weekdayShort(d.date)} ${utcDate(d.date).getUTCDate()}` }),
        );
      }),
    );
    if (target) {
      // Dashed target line, positioned within the bar area (bars use the top ~75% of the chart).
      chart.append(h("span", { class: "target-line", style: `bottom:calc(36px + (100% - 36px) * ${target / max})`, "aria-hidden": "true" }));
    }
    body.replaceChildren(
      chart,
      h("div", { class: "legend" }, h("span", null, h("i", { style: "background:repeating-linear-gradient(45deg,#c3c9ef 0 3px,#e6e8f8 3px 6px)" }), "분석 안 된 기록이 있는 날 (합계 불완전)"), target ? h("span", null, h("i", { style: "background:#e9a08c" }), `열량 목표 ${fmtNum(target)}kcal`) : h("span", { text: "열량 목표 미설정" })),
    );
  }

  function renderCoach() {
    const food = state.food;
    const { day } = food;
    const summary = day.summary;
    const kids = [];
    if (!summary.meal_count) {
      kids.push(emptyState("평가할 기록이 없어요", "이 날짜에 식사를 기록하면 목표와 비교해 알려드려요."));
      $("#coach-body").replaceChildren(...kids);
      return;
    }
    kids.push(h("span", { class: "tag" }, "기록 기반 계산"));
    if (summary.messages.length) {
      kids.push(h("ul", { class: "coach-list" }, summary.messages.map((m) => h("li", { class: m.level, text: m.text }))));
    }
    for (const q of summary.questions) {
      const action =
        q.id === "set_targets" ? btn("목표 설정", "mini-button", openSettings) :
        q.id === "mark_complete" ? btn("모두 기록했어요", "mini-button accent", () => setDayComplete(true)) : null;
      kids.push(h("div", { class: "notice info" }, q.text, action ? h("div", { class: "notice-actions" }, action) : null));
    }

    kids.push(h("p", { class: "sub-title" }, "AI 피드백과 음식 추천 ", h("span", { class: "tag indigo", text: "AI 분석 결과 · 참고용" })));
    if (!state.config.ai_configured) {
      kids.push(h("div", { class: "notice" }, `AI 분석이 설정되지 않아 맞춤 추천을 만들 수 없어요. 서버에 ${state.config.ai_key_name || "AI API 키"}를 설정하면 사용할 수 있어요.`));
      $("#coach-body").replaceChildren(...kids);
      return;
    }
    const coach = day.coach;
    if (coach) {
      if (coach.stale) kids.push(h("div", { class: "notice" }, "이 피드백을 받은 뒤 기록이나 설정이 바뀌었어요. 다시 받으면 최신 기록이 반영돼요."));
      if (coach.summary) kids.push(h("p", { style: "margin:10px 0 0;font-size:13px;line-height:1.65", text: coach.summary }));
      if (coach.observations.length) kids.push(h("ul", { class: "coach-list" }, coach.observations.map((o) => h("li", { text: o }))));
      if (coach.recommendations.length) {
        kids.push(
          h("p", { class: "sub-title", text: "추천 음식" }),
          h("div", { class: "rec-grid" }, coach.recommendations.map((r) => h("div", { class: "rec-card" }, h("strong", { text: r.food }), h("p", { text: `추천 이유 · ${r.reason}` }), r.ingredients.length ? h("p", { text: `주요 재료 · ${r.ingredients.join(", ")}` }) : null))),
        );
      } else {
        kids.push(h("p", { class: "hint", style: "margin-top:8px", text: "이번에는 조건에 맞는 추천 음식이 없어요." }));
      }
      if (coach.filtered_recommendations) kids.push(h("p", { class: "hint", style: "margin-top:6px", text: `알레르기·피하는 음식이 포함된 추천 ${coach.filtered_recommendations}개는 제외했어요.` }));
      if (coach.questions.length) kids.push(h("div", { class: "ai-questions", style: "margin-top:10px" }, h("strong", { text: "더 정확한 피드백을 위해 알려주세요" }), h("ul", null, coach.questions.map((q) => h("li", { text: q })))));
      kids.push(h("p", { class: "hint", style: "margin-top:8px", text: `${fmtStamp(coach.created_at)} 생성 · ${coach.model || "AI"}` }));
    } else {
      kids.push(h("p", { class: "hint", style: "margin-top:8px", text: "목표·알레르기·선호와 이 날짜의 기록을 바탕으로 피드백과 다음 식사 추천을 받아요." }));
    }
    if (food.coachError) kids.push(errorNotice(food.coachError, food.coachError.retryable === false ? null : requestCoach));
    const busy = food.coachBusy === food.date;
    const coachButton = btn(busy ? "분석 중…" : coach ? "피드백 다시 받기" : "AI 피드백과 음식 추천 받기", "secondary-button small-button", requestCoach, { disabled: Boolean(food.coachBusy), style: "margin-top:12px" });
    if (busy) coachButton.prepend(spinner(), " ");
    kids.push(coachButton);
    $("#coach-body").replaceChildren(...kids);
  }

  async function requestCoach() {
    const food = state.food;
    if (food.coachBusy) return;
    const epoch = state.epoch;
    const token = food.token;
    const date = food.date;
    food.coachBusy = date;
    food.coachError = null;
    renderCoach();
    const current = () => epoch === state.epoch && token === state.food.token;
    try {
      const day = await api(`/api/days/${date}/coach`, { method: "POST", body: {}, timeoutMs: AI_TIMEOUT_MS });
      if (current()) food.day = day;
    } catch (error) {
      if (current()) food.coachError = error;
    }
    if (epoch !== state.epoch) return;
    food.coachBusy = false;
    if (food.day) renderFood();
  }

  async function setDayComplete(complete) {
    const food = state.food;
    const epoch = state.epoch;
    const token = food.token;
    const box = $("#day-complete");
    box.checked = complete;
    try {
      const day = await api(`/api/days/${food.date}`, { method: "PUT", body: { complete } });
      if (epoch !== state.epoch || token !== food.token) return;
      food.day = day;
      renderFood();
      notify(complete ? "이 날의 식사를 모두 기록한 것으로 표시했어요." : "‘모두 기록함’ 표시를 해제했어요.");
    } catch (error) {
      box.checked = !complete;
      notify(error.message);
    }
  }

  $("#day-complete").addEventListener("change", (event) => setDayComplete(event.currentTarget.checked));
  $("#food-date").addEventListener("change", (event) => setFoodDate(event.currentTarget.value));
  $("#food-prev-day").addEventListener("click", () => setFoodDate(addDays(state.food.date, -1)));
  $("#food-next-day").addEventListener("click", () => setFoodDate(addDays(state.food.date, 1)));
  $("#food-today").addEventListener("click", () => setFoodDate(todayStr()));

  // ------------------------------------------------------------------ food: meal detail
  function openMealDetail(id) {
    const meal = findMeal(id);
    if (!meal) return;
    const status = state.analyzing.has(id) ? "pending" : meal.nutrition_status;
    const dialog = $("#view-dialog");
    dialog.classList.remove("wide");
    dialog.dataset.view = "meal-detail";
    dialog.dataset.mealId = id;
    $("#view-dialog-title").textContent = `${meal.meal_type_label} · ${fmtDate(meal.date, { month: "long", day: "numeric", weekday: "short" })}${meal.time ? ` ${meal.time}` : ""}`;
    const kids = [];
    if (meal.photo) kids.push(h("img", { class: "full-photo", src: meal.photo.url, alt: `${mealTitle(meal)} 사진` }));
    kids.push(h("div", { class: "detail-meta" }, statusChip(status), meal.nutrition && meal.nutrition.source === "ai" ? h("span", { class: "status-chip", text: `신뢰도 ${CONFIDENCE[meal.nutrition.confidence] || "-"}` }) : null, meal.analyzed_at ? h("span", { class: "status-chip", text: `${fmtStamp(meal.analyzed_at)} 분석` }) : null));

    const n = meal.nutrition;
    const cell = (v) => (v == null ? "정보 없음" : fmtNum(v));
    if (n && n.source === "ai") {
      kids.push(
        h("table", { class: "nutri-table" },
          h("caption", { class: "hint", style: "text-align:left;caption-side:top", text: "AI 추정치 · 실제와 다를 수 있어요" }),
          h("thead", null, h("tr", null, ["음식", "양", "kcal", "탄(g)", "단(g)", "지(g)"].map((t) => h("th", { text: t })))),
          h("tbody", null, n.items.map((item, idx) => h("tr", null, h("td", { text: item.name }), h("td", { text: (meal.items[idx] && meal.items[idx].amount) || "-" }), h("td", { text: cell(item.kcal) }), h("td", { text: cell(item.carbs_g) }), h("td", { text: cell(item.protein_g) }), h("td", { text: cell(item.fat_g) })))),
          h("tfoot", null, h("tr", null, h("td", { text: "합계" }), h("td"), ...NUTRIENTS.map(([k]) => h("td", { text: cell(n.total[k]) })))),
        ),
      );
    } else {
      kids.push(h("table", { class: "nutri-table" }, h("thead", null, h("tr", null, ["음식", "양", "조리법"].map((t) => h("th", { text: t })))), h("tbody", null, meal.items.map((item) => h("tr", null, h("td", { text: item.name }), h("td", { text: item.amount || "-" }), h("td", { text: item.cooking || "-" }))))));
      if (n && n.source === "user") {
        kids.push(h("p", { class: "sub-title", text: "직접 입력한 영양 정보" }), h("p", { class: "hint", text: NUTRIENTS.map(([k, label, unit]) => `${label} ${n.total[k] == null ? "정보 없음" : fmtNum(n.total[k]) + unit}`).join(" · ") }));
      }
      const msg = {
        none: "아직 분석 전이에요. 합계와 그래프에는 포함되지 않아요.",
        pending: "AI가 분석하는 중이에요…",
        failed: `분석에 실패했어요. ${meal.analysis_error || ""}`,
        stale: "음식이나 양이 바뀌어 이전 분석 결과는 사용하지 않아요. 다시 분석해 주세요.",
      }[status];
      if (msg) kids.push(h("div", { class: `notice${status === "failed" || status === "stale" ? " error" : ""}` }, msg));
    }
    const a = meal.analysis;
    if (a) {
      if (a.feedback) kids.push(h("p", { class: "sub-title", text: "이 식사에 대한 피드백" }), h("p", { style: "margin:4px 0 0;font-size:13px;line-height:1.65", text: a.feedback }));
      if (a.assumptions.length) kids.push(h("p", { class: "sub-title", text: "추정할 때 가정한 내용" }), h("ul", { style: "margin:4px 0 0;padding-left:18px;font-size:12px" }, a.assumptions.map((t) => h("li", { text: t }))));
      if (a.questions.length) kids.push(h("div", { class: "ai-questions", style: "margin-top:12px" }, h("strong", { text: "더 정확하게 추정하려면 알려주세요" }), h("ul", null, a.questions.map((q) => h("li", { text: q }))), h("span", { class: "hint", text: "‘수정’에서 양이나 메모를 고친 뒤 다시 분석할 수 있어요." })));
    }
    if (meal.memo) kids.push(h("p", { class: "sub-title", text: "메모" }), h("p", { class: "detail-note", text: meal.memo }));
    $("#view-dialog-body").replaceChildren(...kids);
    const canAnalyze = state.config.ai_configured && status !== "pending";
    $("#view-dialog-foot").replaceChildren(
      btn("삭제", "quiet-button danger-text", () => deleteMeal(meal)),
      btn("수정", "quiet-button", () => { closeDialog(dialog); openMealForm({ meal }); }),
      canAnalyze ? btn(status === "ai" ? "다시 분석" : "AI 분석", "secondary-button", () => { analyzeMeal(meal.id); openMealDetail(meal.id); }) : null,
      btn("닫기", "primary-button", () => closeDialog(dialog)),
    );
    openDialog(dialog);
  }

  // ------------------------------------------------------------------ food: add / edit form
  function addItemRow(item = {}) {
    const container = $("#meal-items");
    const row = h(
      "div",
      { class: "item-row" },
      h("input", { type: "text", maxlength: "80", placeholder: "예: 현미밥", "aria-label": "음식 이름", value: item.name || "" }),
      h("input", { type: "text", maxlength: "80", placeholder: "예: 1공기", "aria-label": "섭취량", value: item.amount || "" }),
      h("input", { type: "text", maxlength: "40", placeholder: "예: 구이", "aria-label": "조리법", value: item.cooking || "" }),
      btn("✕", "icon-small", () => { row.remove(); syncItemRemoveButtons(); }, { "aria-label": "이 음식 삭제" }),
    );
    container.append(row);
    syncItemRemoveButtons();
    return row;
  }
  function syncItemRemoveButtons() {
    const rows = $$("#meal-items .item-row");
    rows.forEach((row) => { row.querySelector("button").disabled = rows.length === 1; });
  }

  function openMealForm({ meal = null, date = null, mealType = null, photo = null, recognition = null, previewUrl = null } = {}) {
    const today = todayStr();
    const isEdit = Boolean(meal);
    state.mealForm = { mode: isEdit ? "edit" : "create", mealId: meal && meal.id, clientId: newId(), photo: isEdit ? meal.photo : photo, original: meal };
    $("#meal-dialog-title").textContent = isEdit ? "식사 기록 수정" : recognition ? "AI 인식 결과 확인" : "식사 기록 추가";
    const form = $("#meal-form");
    form.reset();
    $("#meal-items").replaceChildren();
    const day = isEdit ? meal.date : date || state.food.date || today;
    $("#meal-date").value = day;
    $("#meal-date").max = today;
    const time = isEdit ? meal.time || "" : day === today ? nowTimeStr() : "";
    $("#meal-time").value = time;
    const type = isEdit ? meal.meal_type : mealType || guessMealType(time);
    $$('#meal-form input[name="meal_type"]').forEach((input) => { input.checked = input.value === type; });
    $("#meal-memo").value = isEdit ? meal.memo : "";

    const items = isEdit ? meal.items : recognition && recognition.items.length ? recognition.items : [{}];
    items.forEach((item) => addItemRow(item));

    // Photo preview
    const photoBox = $("#meal-form-photo");
    const photoSrc = previewUrl || (state.mealForm.photo && state.mealForm.photo.thumb_url);
    photoBox.hidden = !photoSrc;
    photoBox.replaceChildren(...(photoSrc ? [h("img", { src: photoSrc, alt: "선택한 음식 사진" }), h("span", { class: "hint", text: isEdit ? "저장된 사진" : "이 사진이 기록에 함께 저장돼요." })] : []));

    // What the AI could not tell from the photo
    const qBox = $("#meal-form-questions");
    const questions = [];
    if (recognition) {
      recognition.items.forEach((item) => {
        if (!item.amount_certain) questions.push(`‘${item.name}’의 양은 사진만으로 알기 어려워요. 실제로 먹은 양을 확인해 주세요.`);
        if (!item.cooking_certain) questions.push(`‘${item.name}’의 조리법(기름·소스 등)을 확인해 주세요.`);
      });
      questions.push(...recognition.questions);
    }
    qBox.hidden = !recognition;
    qBox.replaceChildren(
      ...(recognition
        ? [h("strong", { text: "AI가 인식한 결과예요 · 확인 필요" }), h("p", { style: "margin:4px 0 0", text: "음식 이름·양·조리법이 맞는지 확인하고 고쳐 주세요. 저장 후 이 정보로 영양을 추정해요." }), questions.length ? h("ul", null, questions.map((q) => h("li", { text: q }))) : null]
        : []),
    );

    $("#meal-form-stale").hidden = !(isEdit && meal.nutrition_status === "ai");
    const manual = $("#meal-manual");
    const userNutrition = isEdit && meal.nutrition_status === "user" && meal.nutrition ? meal.nutrition.total : null;
    manual.open = Boolean(userNutrition);
    $("#manual-kcal").value = userNutrition && userNutrition.kcal != null ? userNutrition.kcal : "";
    $("#manual-carbs").value = userNutrition && userNutrition.carbs_g != null ? userNutrition.carbs_g : "";
    $("#manual-protein").value = userNutrition && userNutrition.protein_g != null ? userNutrition.protein_g : "";
    $("#manual-fat").value = userNutrition && userNutrition.fat_g != null ? userNutrition.fat_g : "";

    const ai = state.config.ai_configured;
    $("#meal-save-analyze").hidden = isEdit || !ai;
    $("#meal-save-only").textContent = isEdit ? "수정 저장" : ai ? "분석 없이 저장" : "저장";
    $("#meal-save-only").className = isEdit || !ai ? "primary-button" : "secondary-button";
    showFormError("#meal-form-error", "");
    openDialog($("#meal-dialog"));
    setTimeout(() => { const first = $("#meal-items input"); if (first && !first.value) first.focus(); }, 30);
  }

  $("#meal-add-item").addEventListener("click", () => {
    if ($$("#meal-items .item-row").length >= 12) { notify("한 끼에 음식은 12개까지 기록할 수 있어요."); return; }
    addItemRow().querySelector("input").focus();
  });

  function readMealForm() {
    const type = ($('#meal-form input[name="meal_type"]:checked') || {}).value;
    if (!type) throw new Error("식사 구분(아침·점심·저녁·간식)을 선택해 주세요.");
    const date = $("#meal-date").value;
    if (!date) throw new Error("날짜를 선택해 주세요.");
    if (date > todayStr()) throw new Error("미래 날짜에는 기록할 수 없어요.");
    const items = $$("#meal-items .item-row").map((row) => {
      const [name, amount, cooking] = $$("input", row).map((i) => i.value.trim());
      return { name, amount, cooking };
    }).filter((i) => i.name || i.amount || i.cooking);
    if (!items.length) throw new Error("음식 이름을 한 가지 이상 입력해 주세요.");
    if (items.some((i) => !i.name)) throw new Error("양이나 조리법을 적은 줄에는 음식 이름도 입력해 주세요.");
    const manualValues = { kcal: numOrNull("#manual-kcal"), carbs_g: numOrNull("#manual-carbs"), protein_g: numOrNull("#manual-protein"), fat_g: numOrNull("#manual-fat") };
    if (Object.values(manualValues).some((v) => v != null && Number.isNaN(v))) throw new Error("영양 정보에는 숫자만 입력해 주세요.");
    const hasManual = Object.values(manualValues).some((v) => v != null);
    return { meal_type: type, date, time: $("#meal-time").value || null, items, memo: $("#meal-memo").value, manual_nutrition: hasManual ? manualValues : null };
  }

  $("#meal-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitter = event.submitter || $("#meal-save-only");
    const analyze = submitter.value === "analyze";
    const form = state.mealForm;
    if (!form) return;
    let values;
    try { values = readMealForm(); } catch (error) { showFormError("#meal-form-error", error.message); return; }
    showFormError("#meal-form-error", "");
    const epoch = state.epoch;
    const otherButton = analyze ? $("#meal-save-only") : $("#meal-save-analyze");
    otherButton.disabled = true;
    await withBusy(submitter, "저장 중", async () => {
      try {
        let meal;
        if (form.mode === "create") {
          const res = await api("/api/meals", { method: "POST", body: { ...values, client_id: form.clientId, photo_id: form.photo ? form.photo.id : null } });
          meal = res.meal;
        } else {
          const body = { date: values.date, time: values.time, meal_type: values.meal_type, items: values.items, memo: values.memo };
          if (values.manual_nutrition || form.original.nutrition_status === "user") body.manual_nutrition = values.manual_nutrition;
          const res = await api(`/api/meals/${form.mealId}`, { method: "PATCH", body });
          meal = res.meal;
        }
        if (epoch !== state.epoch) return;
        closeDialog($("#meal-dialog"));
        if (form.mode === "create" && form.photo) resetFlow();
        const moved = meal.date !== state.food.date;
        if (form.mode === "create") {
          notify(analyze ? "식사를 저장했어요. AI 분석을 시작해요." : "식사를 저장했어요.");
          if (moved) { state.food.date = meal.date; await loadFoodDay(); } else await loadFoodDay({ keep: true });
        } else {
          const msg = meal.nutrition_status === "stale" ? "수정했어요. 음식 정보가 바뀌어 재분석이 필요해요." : "수정했어요.";
          notify(moved ? `${msg} ${fmtDate(meal.date)} 기록으로 옮겼어요.` : msg);
          await loadFoodDay({ keep: true });
        }
        loadOverview();
        if (analyze) analyzeMeal(meal.id);
      } catch (error) {
        // Keep the dialog open with the values; retrying reuses the same request id (no duplicates).
        showFormError("#meal-form-error", error.message);
      } finally {
        otherButton.disabled = false;
      }
    });
  });

  // ------------------------------------------------------------------ food: photo → AI recognition flow
  function resetFlow() {
    if (state.flow.prepared) URL.revokeObjectURL(state.flow.prepared.previewUrl);
    state.flow = { ...freshFlow(), token: state.flow.token + 1 };
    $("#food-photo-input").value = "";
    renderFlow();
  }

  $("#food-photo-button").addEventListener("click", () => $("#food-photo-input").click());
  $("#food-photo-input").addEventListener("change", async (event) => {
    const file = event.currentTarget.files && event.currentTarget.files[0];
    event.currentTarget.value = "";
    if (!file) return;
    if (state.flow.prepared) URL.revokeObjectURL(state.flow.prepared.previewUrl);
    // A new token makes any result for the previous photo arrive "too late" and get ignored.
    const flow = { ...freshFlow(), token: state.flow.token + 1, status: "preparing" };
    state.flow = flow;
    renderFlow();
    try {
      flow.prepared = await prepareImage(file);
    } catch (error) {
      if (state.flow !== flow) return;
      flow.status = "failed";
      flow.error = { message: error.message, retryable: false, stage: "prepare" };
      renderFlow();
      return;
    }
    if (state.flow !== flow) { URL.revokeObjectURL(flow.prepared.previewUrl); return; }
    flow.clientId = newId();
    runFlow(flow);
  });

  async function runFlow(flow) {
    const epoch = state.epoch;
    const alive = () => state.flow === flow && epoch === state.epoch;
    flow.error = null;
    try {
      if (!flow.photo) {
        flow.status = "uploading";
        renderFlow();
        const photo = await uploadPhoto("meal", flow.prepared, flow.clientId);
        if (!alive()) return;
        flow.photo = photo;
      }
      if (!state.config.ai_configured) {
        flow.status = "no_ai";
        renderFlow();
        return;
      }
      flow.status = "recognizing";
      renderFlow();
      flow.startedAt = Date.now();
      const res = await api(`/api/photos/${flow.photo.id}/recognize`, { method: "POST", body: {}, timeoutMs: AI_TIMEOUT_MS });
      if (!alive()) return;
      flow.recognition = res.recognition;
      flow.status = res.recognition.status === "recognized" ? "recognized" : "unrecognized";
      renderFlow();
      // Don't overwrite a form the user already opened to type the meal in by hand.
      if (flow.status === "recognized" && !$("#meal-dialog").open) openFlowForm();
    } catch (error) {
      if (!alive()) return;
      flow.status = "failed";
      flow.error = { message: error.message, retryable: error.retryable !== false, stage: flow.photo ? "recognize" : "upload" };
      renderFlow();
    }
  }

  function openFlowForm(manual = false) {
    const flow = state.flow;
    openMealForm({
      date: state.food.date || todayStr(),
      photo: flow.photo,
      recognition: manual ? null : flow.recognition,
      previewUrl: flow.prepared && flow.prepared.previewUrl,
    });
  }

  function renderFlow() {
    const flow = state.flow;
    const box = $("#food-flow");
    if (flow.status === "idle") { box.replaceChildren(); return; }
    const img = flow.prepared ? h("img", { src: flow.prepared.previewUrl, alt: "선택한 음식 사진 미리보기" }) : h("div", { class: "upload-icon", style: "margin:0;width:96px;height:96px" }, "♧");
    let title = "";
    let text = "";
    const actions = [];
    const manualBtn = btn("직접 입력해서 저장", "mini-button", () => openFlowForm(true));
    const cancelBtn = btn("취소", "mini-button", resetFlow);
    switch (flow.status) {
      case "preparing": title = "사진을 준비하는 중이에요…"; break;
      case "uploading": title = "사진을 비공개 저장소에 올리는 중이에요…"; break;
      case "recognizing":
        title = "AI가 음식을 인식하는 중이에요…";
        text = h("span", null, "보통 5~20초 걸려요. ", h("span", { id: "flow-elapsed", text: elapsedText(flow) }));
        actions.push(manualBtn, cancelBtn);
        startFlowTicker();
        break;
      case "recognized":
        title = `인식 완료 · ${flow.recognition.items.map((i) => i.name).join(", ")}`;
        text = "아직 저장 전이에요. 이름·양·조리법을 확인하고 저장해 주세요.";
        actions.push(btn("확인하고 저장하기", "mini-button accent", () => openFlowForm()), cancelBtn);
        break;
      case "unrecognized":
        title = flow.recognition.status === "not_food" ? "사진에서 음식을 찾지 못했어요" : "사진이 흐리거나 어두워 음식을 알아보기 어려워요";
        text = flow.recognition.message || "다른 사진을 선택하거나 직접 입력해 주세요.";
        actions.push(btn("다른 사진 선택", "mini-button accent", () => $("#food-photo-input").click()), btn("다시 인식", "mini-button", () => runFlow(flow)), manualBtn, cancelBtn);
        break;
      case "no_ai":
        title = "사진을 첨부했어요 · AI 분석 미설정";
        text = "AI 분석이 설정되지 않아 음식 정보는 직접 입력해 주세요. 사진은 기록에 함께 저장돼요.";
        actions.push(btn("직접 입력해서 저장", "mini-button accent", () => openFlowForm(true)), cancelBtn);
        break;
      case "failed":
        title = flow.error.stage === "upload" ? "사진을 올리지 못했어요" : flow.error.stage === "prepare" ? "사진을 열 수 없어요" : "AI 분석에 실패했어요";
        text = flow.error.message;
        if (flow.error.retryable && flow.prepared) actions.push(btn("다시 시도", "mini-button accent", () => runFlow(flow)));
        actions.push(btn("다른 사진 선택", "mini-button", () => $("#food-photo-input").click()));
        if (flow.error.stage !== "prepare") actions.push(manualBtn);
        actions.push(cancelBtn);
        break;
      default: break;
    }
    const busy = ["preparing", "uploading", "recognizing"].includes(flow.status);
    box.replaceChildren(
      h("div", { class: "flow-card" }, img, h("div", null, h("strong", null, busy ? spinner() : null, busy ? " " : null, title), text ? h("p", null, text) : null, actions.length ? h("div", { class: "flow-actions" }, actions) : null)),
    );
  }

  function elapsedText(flow) {
    if (!flow.startedAt) return "";
    const seconds = Math.floor((Date.now() - flow.startedAt) / 1000);
    return seconds >= 25 ? `${seconds}초째 · 사용자가 많아 늦어지고 있어요. 기다리거나 직접 입력할 수 있어요.` : `${seconds}초째`;
  }

  let flowTicker = null;
  function startFlowTicker() {
    if (flowTicker) return;
    flowTicker = setInterval(() => {
      const el = $("#flow-elapsed");
      if (state.flow.status !== "recognizing" || !el) { clearInterval(flowTicker); flowTicker = null; return; }
      el.textContent = elapsedText(state.flow);
    }, 1000);
  }

  // ------------------------------------------------------------------ body log: calendar
  function monthGrid(monthStart) {
    const first = utcDate(monthStart);
    const offset = (first.getUTCDay() + 6) % 7;
    const gridStart = addDays(monthStart, -offset);
    const [y, m] = ymd(monthStart);
    const daysInMonth = new Date(Date.UTC(y, m, 0)).getUTCDate();
    const cells = Math.ceil((offset + daysInMonth) / 7) * 7;
    return { gridStart, gridEnd: addDays(gridStart, cells - 1), cells };
  }

  async function loadBodyMonth() {
    const body = state.body;
    const epoch = state.epoch;
    const token = ++body.token;
    const { gridStart, gridEnd } = monthGrid(body.month);
    body.loaded = false;
    body.error = null;
    body.logsByDate = new Map();
    renderCalendar();
    renderBodyDay();
    try {
      const res = await api(`/api/body-logs?from=${gridStart}&to=${gridEnd}&order=asc`);
      if (epoch !== state.epoch || token !== body.token) return;
      for (const log of res.body_logs) {
        if (!body.logsByDate.has(log.date)) body.logsByDate.set(log.date, []);
        body.logsByDate.get(log.date).push(log);
      }
      body.loaded = true;
    } catch (error) {
      if (epoch !== state.epoch || token !== body.token) return;
      body.error = error;
    }
    renderCalendar();
    renderBodyDay();
  }

  function renderCalendar() {
    const body = state.body;
    const [y, m] = ymd(body.month);
    $("#calendar-month").textContent = `${y}년 ${m}월`;
    const today = todayStr();
    const { gridStart, cells } = monthGrid(body.month);
    const grid = $("#calendar-grid");
    const kids = ["월", "화", "수", "목", "금", "토", "일"].map((label) => h("span", { class: "calendar-label", text: label }));
    for (let i = 0; i < cells; i += 1) {
      const date = addDays(gridStart, i);
      const outside = date.slice(0, 7) !== body.month.slice(0, 7);
      const count = (body.logsByDate.get(date) || []).length;
      const cls = ["calendar-day", outside ? "outside" : "", count ? "has-entry" : "", date === today ? "today" : "", date === body.selected ? "selected" : ""].join(" ");
      kids.push(
        h("button", { type: "button", class: cls, "aria-label": `${fmtDateLong(date)}${count ? `, 기록 ${count}개` : ", 기록 없음"}`, "aria-pressed": String(date === body.selected), onclick: () => selectBodyDate(date) }, String(utcDate(date).getUTCDate())),
      );
    }
    grid.replaceChildren(...kids);
    $("#calendar-caption").textContent = body.error ? body.error.message : body.loaded ? "저장된 기록이 있는 날만 작은 점으로 표시돼요." : "기록을 불러오는 중이에요…";
  }

  function selectBodyDate(date) {
    state.body.selected = date;
    renderCalendar();
    renderBodyDay();
  }

  function renderBodyDay() {
    const body = state.body;
    const box = $("#body-day");
    if (!body.selected) {
      box.replaceChildren(h("p", { class: "hint", text: "날짜를 누르면 그 날의 사진과 메모를 볼 수 있어요." }));
      return;
    }
    if (body.error) { box.replaceChildren(errorNotice(body.error, loadBodyMonth)); return; }
    if (!body.loaded) { box.replaceChildren(loadingLine("기록을 불러오는 중이에요…")); return; }
    const date = body.selected;
    const logs = body.logsByDate.get(date) || [];
    const future = date > todayStr();
    const addButton = future ? null : btn("이 날짜에 기록 추가", "mini-button accent", () => openBodyForm({ date }));
    const head = h("div", { class: "body-day-head" }, h("strong", { text: fmtDateLong(date) }), logs.length ? addButton : null);
    if (!logs.length) {
      box.replaceChildren(head, h("div", { style: "margin-top:10px" }, emptyState("이 날짜의 기록이 없어요", future ? "미래 날짜에는 기록할 수 없어요." : "사진이나 메모로 이 날을 남길 수 있어요.", addButton)));
      return;
    }
    box.replaceChildren(head, h("div", { class: "body-day-list" }, logs.map((log, idx) => bodyLogCard(log, idx, logs.length))));
  }

  function bodyLogCard(log, idx, total) {
    return h(
      "div",
      { class: "log-card" },
      h("button", { type: "button", class: "thumb", "aria-label": "기록 상세 보기", onclick: () => openBodyDetail(log) }, log.photo ? h("img", { src: log.photo.thumb_url, alt: "", loading: "lazy" }) : "✎"),
      h(
        "div",
        { style: "min-width:0" },
        h("small", { text: `${total > 1 ? `${idx + 1}/${total} · ` : ""}${fmtStamp(log.created_at)} 저장됨` }),
        h("p", { text: log.note || "(메모 없음)" }),
        h("div", { class: "entry-actions", style: "margin-top:0" }, btn("상세", "mini-button", () => openBodyDetail(log)), btn("수정", "mini-button", () => openBodyForm({ log })), btn("삭제", "mini-button danger", () => deleteBodyLog(log))),
      ),
    );
  }

  $("#calendar-prev").addEventListener("click", () => { state.body.month = addDays(state.body.month, -1).slice(0, 8) + "01"; loadBodyMonth(); });
  $("#calendar-next").addEventListener("click", () => { state.body.month = addDays(addDays(state.body.month, 31).slice(0, 8) + "01", 0); loadBodyMonth(); });

  // ------------------------------------------------------------------ body log: form
  function openBodyForm({ log = null, date = null, pickPhoto = false } = {}) {
    const today = todayStr();
    if (state.bodyForm && state.bodyForm.prepared) URL.revokeObjectURL(state.bodyForm.prepared.previewUrl);
    state.bodyForm = {
      mode: log ? "edit" : "create",
      log,
      clientId: newId(),
      photoClientId: null,
      prepared: null,
      uploaded: null,
      existing: log ? log.photo : null,
      removeExisting: false,
      preparing: false,
    };
    $("#body-dialog-title").textContent = log ? "바디 로그 수정" : "바디 로그 추가";
    $("#body-date").value = log ? log.date : date || state.body.selected || today;
    if ($("#body-date").value > today) $("#body-date").value = today;
    $("#body-date").max = today;
    $("#body-note").value = log ? log.note : "";
    showFormError("#body-form-error", "");
    renderBodyPhotoArea();
    openDialog($("#body-dialog"));
    if (pickPhoto) $("#body-photo-input").click();
  }

  function renderBodyPhotoArea() {
    const f = state.bodyForm;
    const area = $("#body-photo-area");
    const pick = btn(f.prepared || (f.existing && !f.removeExisting) ? "다른 사진 선택" : "사진 선택", "mini-button accent", () => $("#body-photo-input").click());
    if (f.preparing) { area.replaceChildren(loadingLine("사진을 준비하는 중이에요…")); return; }
    if (f.prepared) {
      area.replaceChildren(
        h("div", { class: "form-photo" }, h("img", { src: f.prepared.previewUrl, alt: "선택한 사진 미리보기" }), h("div", null, h("span", { class: "status-chip pending", text: "미리보기 · 아직 저장되지 않았어요" }), h("div", { class: "flow-actions" }, pick, btn("사진 빼기", "mini-button", () => { URL.revokeObjectURL(f.prepared.previewUrl); f.prepared = null; f.uploaded = null; renderBodyPhotoArea(); })))),
      );
      return;
    }
    if (f.existing && !f.removeExisting) {
      area.replaceChildren(
        h("div", { class: "form-photo" }, h("img", { src: f.existing.thumb_url, alt: "저장된 사진" }), h("div", null, h("span", { class: "status-chip user", text: "저장된 사진" }), h("div", { class: "flow-actions" }, pick, btn("사진 빼기", "mini-button", () => { f.removeExisting = true; renderBodyPhotoArea(); })))),
      );
      return;
    }
    area.replaceChildren(h("div", { class: "flow-actions", style: "margin-top:0" }, pick), h("p", { class: "hint", style: "margin-top:6px", text: f.existing ? "저장하면 기존 사진이 기록에서 빠지고 삭제돼요." : "사진 없이 메모만 저장해도 괜찮아요." }));
  }

  $("#body-photo-input").addEventListener("change", async (event) => {
    const file = event.currentTarget.files && event.currentTarget.files[0];
    event.currentTarget.value = "";
    const f = state.bodyForm;
    if (!file || !f) return;
    f.preparing = true;
    renderBodyPhotoArea();
    try {
      const prepared = await prepareImage(file);
      if (state.bodyForm !== f) { URL.revokeObjectURL(prepared.previewUrl); return; }
      if (f.prepared) URL.revokeObjectURL(f.prepared.previewUrl);
      f.prepared = prepared;
      f.photoClientId = newId();
      f.uploaded = null;
      showFormError("#body-form-error", "");
    } catch (error) {
      showFormError("#body-form-error", error.message);
    } finally {
      f.preparing = false;
      if (state.bodyForm === f) renderBodyPhotoArea();
    }
  });

  $("#body-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const f = state.bodyForm;
    if (!f || f.preparing) return;
    const date = $("#body-date").value;
    const note = $("#body-note").value.trim();
    if (!date) { showFormError("#body-form-error", "날짜를 선택해 주세요."); return; }
    if (date > todayStr()) { showFormError("#body-form-error", "미래 날짜에는 기록할 수 없어요."); return; }
    const willHavePhoto = Boolean(f.prepared) || (f.existing && !f.removeExisting);
    if (!willHavePhoto && !note) { showFormError("#body-form-error", "사진이나 메모 중 하나는 남겨 주세요."); return; }
    showFormError("#body-form-error", "");
    const epoch = state.epoch;
    await withBusy($("#body-save"), "저장 중", async () => {
      try {
        if (f.prepared && !f.uploaded) f.uploaded = await uploadPhoto("body", f.prepared, f.photoClientId);
        let saved;
        if (f.mode === "create") {
          const res = await api("/api/body-logs", { method: "POST", body: { client_id: f.clientId, date, note, photo_id: f.uploaded ? f.uploaded.id : null } });
          saved = res.body_log;
        } else {
          const body = { date, note };
          if (f.uploaded) body.photo_id = f.uploaded.id;
          else if (f.removeExisting) body.photo_id = null;
          const res = await api(`/api/body-logs/${f.log.id}`, { method: "PATCH", body });
          saved = res.body_log;
        }
        if (epoch !== state.epoch) return;
        if (f.prepared) URL.revokeObjectURL(f.prepared.previewUrl);
        state.bodyForm = null;
        closeDialog($("#body-dialog"));
        notify(f.mode === "create" ? `${fmtDate(saved.date)} 바디 로그를 저장했어요.` : "바디 로그를 수정했어요.");
        state.body.month = saved.date.slice(0, 8) + "01";
        state.body.selected = saved.date;
        await loadBodyMonth();
        loadComparePreview();
        loadOverview();
        if ($("#view-dialog").open && $("#view-dialog").dataset.view === "gallery") loadGallery();
      } catch (error) {
        showFormError("#body-form-error", `${error.message} 저장되지 않았어요.`);
      }
    });
  });

  async function deleteBodyLog(log) {
    const ok = await confirmBox(`${fmtDate(log.date)} 바디 로그를 삭제할까요? 사진도 함께 삭제돼요.`, "삭제");
    if (!ok) return;
    const epoch = state.epoch;
    try {
      await api(`/api/body-logs/${log.id}`, { method: "DELETE" });
      if (epoch !== state.epoch) return;
      notify("바디 로그를 삭제했어요.");
      const view = $("#view-dialog");
      if (view.open && view.dataset.view === "body-detail") closeDialog(view);
      loadBodyMonth();
      loadComparePreview();
      loadOverview();
      if (view.open && view.dataset.view === "gallery") loadGallery();
    } catch (error) {
      notify(error.message);
    }
  }

  // ------------------------------------------------------------------ body log: detail, gallery, compare
  function openBodyDetail(log, { fromGallery = false } = {}) {
    const dialog = $("#view-dialog");
    dialog.dataset.view = fromGallery ? "gallery" : "body-detail";
    dialog.dataset.mealId = "";
    dialog.classList.toggle("wide", false);
    $("#view-dialog-title").textContent = `${fmtDateLong(log.date)} 바디 로그`;
    $("#view-dialog-body").replaceChildren(
      log.photo ? h("img", { class: "full-photo", src: log.photo.url, alt: `${fmtDate(log.date)} 바디 로그 사진` }) : h("div", { class: "empty-state" }, "사진 없이 메모만 저장한 기록이에요."),
      h("div", { class: "detail-meta" }, h("span", { class: "status-chip user", text: "저장됨" }), h("span", { class: "status-chip", text: `${fmtStamp(log.created_at)} 저장` }), log.updated_at !== log.created_at ? h("span", { class: "status-chip", text: `${fmtStamp(log.updated_at)} 수정` }) : null),
      h("p", { class: "detail-note", text: log.note || "(메모 없음)" }),
    );
    $("#view-dialog-foot").replaceChildren(
      fromGallery ? btn("모아보기로 돌아가기", "quiet-button", renderGallery) : null,
      btn("삭제", "quiet-button danger-text", () => deleteBodyLog(log)),
      btn("수정", "quiet-button", () => { closeDialog(dialog); openBodyForm({ log }); }),
      btn("닫기", "primary-button", () => closeDialog(dialog)),
    );
    openDialog(dialog);
  }

  function openGallery(compare = false) {
    const g = state.gallery;
    g.compare = compare;
    g.picked = [];
    const dialog = $("#view-dialog");
    dialog.dataset.view = "gallery";
    dialog.dataset.mealId = "";
    dialog.classList.add("wide");
    openDialog(dialog);
    loadGallery();
  }

  async function loadGallery() {
    const g = state.gallery;
    const epoch = state.epoch;
    const token = ++g.token;
    g.logs = null;
    g.error = null;
    renderGallery();
    const params = new URLSearchParams({ order: g.order });
    if (g.from) params.set("from", g.from);
    if (g.to) params.set("to", g.to);
    try {
      const res = await api(`/api/body-logs?${params}`);
      if (epoch !== state.epoch || token !== g.token) return;
      g.logs = res.body_logs;
      g.picked = g.picked.filter((id) => g.logs.some((l) => l.id === id));
    } catch (error) {
      if (epoch !== state.epoch || token !== g.token) return;
      g.error = error;
    }
    renderGallery();
  }

  function renderGallery() {
    const g = state.gallery;
    const dialog = $("#view-dialog");
    dialog.dataset.view = "gallery";
    dialog.classList.add("wide");
    $("#view-dialog-title").textContent = g.compare ? "비교할 두 기록 고르기" : "나의 기록 모아보기";
    const orderSelect = h("select", { "aria-label": "정렬", onchange: (e) => { g.order = e.currentTarget.value; loadGallery(); } }, h("option", { value: "desc", text: "최신순" }), h("option", { value: "asc", text: "과거순" }));
    orderSelect.value = g.order;
    const fromInput = h("input", { type: "date", value: g.from, max: todayStr(), onchange: (e) => { g.from = e.currentTarget.value; loadGallery(); } });
    const toInput = h("input", { type: "date", value: g.to, max: todayStr(), onchange: (e) => { g.to = e.currentTarget.value; loadGallery(); } });
    const toolbar = h(
      "div",
      { class: "toolbar" },
      h("label", null, "정렬", orderSelect),
      h("label", null, "시작일", fromInput),
      h("label", null, "종료일", toInput),
      g.from || g.to ? btn("기간 초기화", "mini-button", () => { g.from = ""; g.to = ""; loadGallery(); }) : null,
      btn(g.compare ? "비교 모드 끄기" : "두 기록 비교하기", g.compare ? "mini-button accent" : "mini-button", () => { g.compare = !g.compare; g.picked = []; renderGallery(); }),
    );
    const kids = [toolbar];
    if (g.error) kids.push(errorNotice(g.error, loadGallery));
    else if (!g.logs) kids.push(loadingLine("기록을 불러오는 중이에요…"));
    else if (!g.logs.length) {
      kids.push(
        g.from || g.to
          ? emptyState("이 기간에는 기록이 없어요", "기간을 바꾸거나 초기화해 보세요.")
          : emptyState("아직 저장된 바디 로그가 없어요", "첫 기록을 남기면 여기에 모여요.", btn("기록 추가", "primary-button small-button", () => { closeDialog(dialog); openBodyForm({}); })),
      );
    } else {
      if (g.compare) {
        kids.push(h("div", { class: "notice info", style: "margin:0 0 12px" }, g.logs.length < 2 ? "비교하려면 기록이 2개 이상 필요해요. 기간을 넓히거나 기록을 더 남겨 주세요." : `비교할 기록 두 개를 골라 주세요. (${g.picked.length}/2 선택)`));
      }
      kids.push(
        h("p", { class: "hint", style: "margin:0 0 8px", text: `기록 ${g.logs.length}개` }),
        h(
          "div",
          { class: "gallery-grid" },
          g.logs.map((log) => {
            const pickedIdx = g.picked.indexOf(log.id);
            return h(
              "button",
              {
                type: "button",
                class: `gallery-card${pickedIdx >= 0 ? " picked" : ""}`,
                "aria-pressed": g.compare ? String(pickedIdx >= 0) : null,
                "aria-label": `${fmtDateLong(log.date)} 기록${log.photo ? " (사진 있음)" : " (메모만)"}`,
                onclick: () => {
                  if (!g.compare) { openBodyDetail(log, { fromGallery: true }); return; }
                  if (pickedIdx >= 0) g.picked.splice(pickedIdx, 1);
                  else if (g.picked.length < 2) g.picked.push(log.id);
                  else notify("두 개까지만 고를 수 있어요. 선택을 해제한 뒤 골라 주세요.");
                  renderGallery();
                },
              },
              pickedIdx >= 0 ? h("span", { class: "pick-badge", text: String(pickedIdx + 1) }) : null,
              h("span", { class: `g-thumb${log.photo ? " has-img" : ""}` }, log.photo ? h("img", { src: log.photo.thumb_url, alt: "", loading: "lazy" }) : (log.note || "").slice(0, 60) || "메모"),
              h("span", { class: "g-date", text: fmtDate(log.date, { year: "numeric", month: "short", day: "numeric" }) }),
            );
          }),
        ),
      );
    }
    $("#view-dialog-body").replaceChildren(...kids);
    const foot = [];
    if (g.compare) {
      foot.push(btn("선택 해제", "quiet-button", () => { g.picked = []; renderGallery(); }, { disabled: !g.picked.length }));
      foot.push(btn("선택한 두 기록 비교", "primary-button", renderCompare, { disabled: g.picked.length !== 2 }));
    } else {
      foot.push(btn("닫기", "primary-button", () => closeDialog(dialog)));
    }
    $("#view-dialog-foot").replaceChildren(...foot);
  }

  function compareFrame(log, label) {
    if (!log.photo) {
      return h("div", { class: "compare-frame memo-only" }, h("div", null, h("strong", { style: "display:block;font-size:12px", text: `${label} · ${fmtDate(log.date, { year: "numeric", month: "long", day: "numeric" })}` }), h("p", { style: "margin:8px 0 0;white-space:pre-wrap", text: log.note || "(메모 없음)" }), h("small", { text: "사진 없이 저장된 기록" })));
    }
    return h("div", { class: "compare-frame has-image" }, h("img", { src: log.photo.url, alt: `${label} 사진` }), log.note ? h("div", { class: "compare-note", text: log.note.slice(0, 80) }) : null, h("span", { text: `${label} · ${fmtDate(log.date, { year: "numeric", month: "long", day: "numeric" })}` }));
  }

  function renderCompare() {
    const g = state.gallery;
    const picked = g.picked.map((id) => g.logs.find((l) => l.id === id)).filter(Boolean);
    if (picked.length !== 2) return;
    picked.sort((a, b) => (a.date === b.date ? a.created_at.localeCompare(b.created_at) : a.date.localeCompare(b.date)));
    const [a, b] = picked;
    const gap = daysBetween(a.date, b.date);
    $("#view-dialog-title").textContent = "두 기록 비교";
    $("#view-dialog-body").replaceChildren(
      h("div", { class: "compare-stage" }, compareFrame(a, "이전"), compareFrame(b, "이후")),
      h("p", { class: "hint", style: "margin-top:12px", text: gap === 0 ? "같은 날의 두 기록이에요." : `두 기록은 ${gap}일 차이가 나요.` }),
    );
    $("#view-dialog-foot").replaceChildren(btn("다시 고르기", "quiet-button", renderGallery), btn("닫기", "primary-button", () => closeDialog($("#view-dialog"))));
  }

  function resetComparePreview() {
    $("#compare-preview").replaceChildren(h("div", { class: "compare-frame" }, h("span", { text: "첫 기록" })), h("div", { class: "compare-frame" }, h("span", { text: "최근 기록" })));
  }

  let comparePreviewToken = 0;
  async function loadComparePreview() {
    const epoch = state.epoch;
    const token = ++comparePreviewToken; // only the newest request may update the preview
    const current = () => epoch === state.epoch && token === comparePreviewToken;
    try {
      const res = await api("/api/body-logs?order=asc");
      if (!current()) return;
      const logs = res.body_logs;
      const withPhoto = logs.filter((l) => l.photo);
      if (withPhoto.length >= 2) {
        const first = withPhoto[0];
        const latest = withPhoto[withPhoto.length - 1];
        $("#compare-preview").replaceChildren(compareFrame(first, "첫 기록"), compareFrame(latest, "최근 기록"));
      } else if (withPhoto.length === 1) {
        // Show the only photo right away instead of waiting for a second one.
        $("#compare-preview").replaceChildren(
          compareFrame(withPhoto[0], "첫 기록"),
          h("div", { class: "compare-frame" }, h("div", { style: "padding:0 12px;text-align:center" }, h("span", { text: "최근 기록" }), h("p", { style: "margin:8px 0 0;line-height:1.5", text: "사진을 하나 더 올리면 여기에 보여요" }))),
        );
      } else {
        resetComparePreview();
      }
      $("#compare-note").textContent = !logs.length
        ? "아직 저장된 기록이 없어요. 첫 기록을 남기면 여기서 모아 볼 수 있어요."
        : !withPhoto.length
          ? `저장된 기록 ${logs.length}개 · 사진을 올리면 여기에 바로 보여요.`
          : withPhoto.length === 1
            ? `저장된 기록 ${logs.length}개 · 사진이 있는 기록이 하나 더 생기면 첫 기록과 최근 기록을 나란히 보여드려요.`
            : `저장된 기록 ${logs.length}개 · 사진이 있는 첫 기록과 최근 기록이에요. 원하는 두 기록을 골라 비교할 수도 있어요.`;
    } catch (error) {
      if (!current()) return;
      $("#compare-note").textContent = error.message;
    }
  }

  // ------------------------------------------------------------------ body: workout routine
  async function loadWorkout() {
    const w = state.workout;
    const epoch = state.epoch;
    const token = ++w.token;
    w.error = null;
    if (!w.data) renderRoutine();
    try {
      const data = await api("/api/workout-plan");
      if (epoch !== state.epoch || token !== w.token) return;
      w.data = data;
    } catch (error) {
      if (epoch !== state.epoch || token !== w.token) return;
      w.error = error;
    }
    renderRoutine();
  }

  function renderRoutine() {
    const w = state.workout;
    const box = $("#routine-body");
    if (w.error) { box.replaceChildren(errorNotice(w.error, loadWorkout)); return; }
    if (!w.data) { box.replaceChildren(loadingLine("몸 상태를 불러오는 중이에요…")); return; }
    const { body, plan } = w.data;
    const kids = [];
    const stat = (label, value, note) => h("div", { class: "stat-card" }, h("span", { text: label }), h("strong", { text: value }), h("small", { text: note }));
    const v = (x, unit) => (x == null ? "–" : `${fmt1(x)}${unit}`);
    kids.push(
      h("div", { class: "body-stats" },
        stat("키", v(body.height_cm, "cm"), "직접 입력"),
        stat("몸무게", v(body.weight_kg, "kg"), "직접 입력"),
        stat("골격근량", v(body.skeletal_muscle_kg, "kg"), body.skeletal_muscle_kg == null ? "측정값이 있으면 입력" : "직접 입력"),
        stat("BMI", body.bmi == null ? "–" : fmt1(body.bmi), "키·몸무게로 계산"),
        stat("골격근 비율", v(body.muscle_ratio_pct, "%"), "골격근량÷몸무게로 계산"),
      ),
      h("p", { class: "hint", style: "margin-top:8px", text: `${body.updated_at ? `${fmtStamp(body.updated_at)} 업데이트 · ` : ""}BMI는 근육량을 반영하지 못하는 참고 지표예요.${body.condition_note ? ` · 주의할 점: ${body.condition_note}` : ""}` }),
    );
    if (body.height_cm == null || body.weight_kg == null) {
      kids.push(h("div", { style: "margin-top:12px" }, emptyState("키와 몸무게를 입력하면 운동 루틴을 추천해 드려요", "개인 설정의 ‘현재 몸 상태’에 입력할 수 있어요. 골격근량도 있으면 더 맞춤화돼요.", btn("몸 상태 입력", "mini-button accent", () => openSettings({ focus: "#set-height" })))));
      box.replaceChildren(...kids);
      return;
    }

    kids.push(h("p", { class: "sub-title" }, "이번 주 운동 루틴 ", h("span", { class: "tag indigo", text: "AI 추천 · 참고용" })));
    if (!state.config.ai_configured) {
      kids.push(h("div", { class: "notice", style: "margin-top:8px" }, `AI 분석이 설정되지 않아 운동 루틴을 추천할 수 없어요. 서버에 ${state.config.ai_key_name || "AI API 키"}를 설정하면 사용할 수 있어요.`));
      box.replaceChildren(...kids);
      return;
    }
    if (plan) {
      if (plan.stale) kids.push(h("div", { class: "notice", style: "margin-top:8px" }, "이 루틴을 받은 뒤 몸 상태나 운동 설정이 바뀌었어요. 다시 받으면 최신 값이 반영돼요."));
      if (plan.summary) kids.push(h("p", { style: "margin:10px 0 0;font-size:13px;line-height:1.65", text: plan.summary }));
      kids.push(
        h("div", { class: "session-grid" }, plan.sessions.map((s) => h("div", { class: "session-card" },
          h("h4", { text: s.title }),
          h("p", { class: "session-meta", text: [s.focus, `${s.minutes}분`, s.intensity].filter(Boolean).join(" · ") }),
          h("ol", null, s.exercises.map((ex) => h("li", null, `${ex.name}${ex.amount ? ` · ${ex.amount}` : ""}`, ex.tip ? h("small", { text: ex.tip }) : null))),
        ))),
      );
      if (plan.cautions.length) kids.push(h("ul", { class: "coach-list" }, plan.cautions.map((c) => h("li", { text: c }))));
      if (plan.filtered_exercises) kids.push(h("p", { class: "hint", style: "margin-top:6px", text: `안전하지 않은 표현이 담긴 동작 ${plan.filtered_exercises}개는 제외했어요.` }));
      if (plan.questions.length) kids.push(h("div", { class: "ai-questions", style: "margin-top:10px" }, h("strong", { text: "더 맞춤화된 루틴을 위해 알려주세요" }), h("ul", null, plan.questions.map((q) => h("li", { text: q })))));
      kids.push(h("p", { class: "hint", style: "margin-top:8px", text: `${fmtStamp(plan.created_at)} 생성 · ${plan.model || "AI"} · 통증이 있으면 멈추고, 질환이 있다면 전문가와 먼저 상의하세요.` }));
    } else {
      kids.push(h("p", { class: "hint", style: "margin-top:8px", text: "몸 상태와 목표·운동 경험·장비·운동 계획을 바탕으로 이번 주 루틴을 만들어요." }));
    }
    if (w.requestError) kids.push(errorNotice(w.requestError, w.requestError.retryable === false ? null : requestWorkout));
    const button = btn(w.busy ? "추천 중…" : plan ? "루틴 다시 받기" : "운동 루틴 추천 받기", "secondary-button small-button", requestWorkout, { disabled: w.busy, style: "margin-top:12px" });
    if (w.busy) button.prepend(spinner(), " ");
    kids.push(button);
    box.replaceChildren(...kids);
  }

  async function requestWorkout() {
    const w = state.workout;
    if (w.busy) return;
    const epoch = state.epoch;
    w.busy = true;
    w.requestError = null;
    renderRoutine();
    try {
      const data = await api("/api/workout-plan", { method: "POST", body: {}, timeoutMs: AI_TIMEOUT_MS });
      if (epoch !== state.epoch) return;
      w.data = data;
      w.token += 1; // a slower GET started earlier must not overwrite the new plan
    } catch (error) {
      if (epoch !== state.epoch) return;
      w.requestError = error;
    }
    w.busy = false;
    renderRoutine();
  }

  // ------------------------------------------------------------------ global events
  document.addEventListener("click", (event) => {
    const screenButton = event.target.closest("[data-screen]");
    if (screenButton && !screenButton.closest("dialog")) {
      if (screenButton.id === "today-date-button") { goToFood(todayStr()); return; }
      showScreen(screenButton.dataset.screen);
      return;
    }
    const closer = event.target.closest("[data-close]");
    if (closer) { closeDialog(closer.closest("dialog")); return; }
    const actionEl = event.target.closest("[data-action]");
    if (!actionEl || !state.user) return;
    const action = actionEl.dataset.action;
    if (action === "logout") logout();
    else if (action === "open-settings") openSettings();
    else if (action === "edit-body") openSettings({ focus: "#set-height" });
    else if (action === "add-meal") openMealForm({ date: state.food.date || todayStr() });
    else if (action === "add-meal-today") goToFood(todayStr(), true);
    else if (action === "body-new-photo") openBodyForm({ date: todayStr(), pickPhoto: true });
    else if (action === "body-new-note") openBodyForm({ date: todayStr() });
    else if (action === "open-gallery") openGallery(false);
    else if (action === "open-compare") openGallery(true);
  });

  $("#meal-dialog").addEventListener("close", () => { state.mealForm = null; });
  $("#body-dialog").addEventListener("close", () => {
    const f = state.bodyForm;
    if (f && f.prepared) URL.revokeObjectURL(f.prepared.previewUrl);
    state.bodyForm = null;
  });
  $("#notification-button").addEventListener("click", () => notify("알림 설정은 ‘내 기록’에서 바꿀 수 있어요. (알림 발송 기능은 아직 없어요)"));
  $("#badge-info").addEventListener("click", () => notify("배지는 저장된 기록으로 자동으로 열려요: 첫 기록, 4일 이상 쉬었다가 다시 기록, 한 주에 3일 이상 기록."));
  $("#privacy-info").addEventListener("click", () => notify(`기록과 사진은 이 서버에 내 계정으로만 저장돼요. AI 분석을 요청할 때만 사진과 식사 정보가 AI 서비스(${(state.config && state.config.ai_provider_name) || "AI 서비스"})로 전송돼요.`));

  // ------------------------------------------------------------------ boot
  async function boot() {
    renderMissions(null);
    resetComparePreview();
    try {
      state.config = await api("/api/config");
    } catch (error) {
      authStatus(error.message, true);
      return;
    }
    try {
      const me = await api("/api/me");
      await onSignedIn(me);
    } catch (error) {
      showSignedOut(error.status === 401 ? null : error.message);
    }
  }

  boot();
})();
