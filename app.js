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
  // replaceChildren() prints null/false as the text "null"/"false"; this skips them (like h() does).
  const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));
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
  const WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]; // Mon=0, same as the server
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
    loadNutrition();
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
    state.nutrition = null;
    state.explaining = false;
    state.explainError = null;
    state.feedbackDraft = null;
    if (reviewTimer) { clearTimeout(reviewTimer); reviewTimer = null; }
    $("#today-review").replaceChildren();
    $("#today-feedback").replaceChildren();
    state.mealForm = null;
    state.bodyForm = null;
    state.analyzing = new Set();
    ["#meal-list", "#nutrition-body", "#trend-body", "#coach-body", "#food-flow", "#calendar-grid", "#body-day", "#settings-summary", "#week-days", "#view-dialog-body", "#view-dialog-foot", "#routine-body", "#challenge-list", "#today-workout", "#today-nutrition", "#nutrition-panel"].forEach((sel) => $(sel).replaceChildren());
    $("#challenge-level").textContent = "–";
    $("#challenge-progress").textContent = "";
    $("#challenge-basis").textContent = "기록을 불러오는 중이에요…";
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
    renderTodayGoal(null);
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
    if (name === "profile") loadNutrition();
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
    renderTodayGoal(ov.today_workout);
    renderTodayNutrition(ov.nutrition);

    $("#stat-movement").textContent = `${ov.month.movement}회`;
    $("#stat-meals").textContent = `${ov.month.meals}회`;
    $("#stat-body").textContent = `${ov.month.body_logs}회`;
    $("#stat-days").textContent = `${ov.month.record_days}일`;
    $$(".badge-card[data-badge]").forEach((card) => {
      const earned = Boolean(ov.badges[card.dataset.badge]);
      card.classList.toggle("locked", !earned);
      card.setAttribute("aria-label", `${card.querySelector("strong").textContent} ${earned ? "획득" : "아직 잠김"}`);
    });
    renderChallenges(ov.challenges);
  }

  function renderChallenges(ch) {
    if (!ch) return;
    $("#challenge-level").textContent = `내 수준 · ${ch.level}`;
    $("#challenge-progress").textContent = `${ch.done_count} / ${ch.total} 달성`;
    $("#challenge-basis").textContent = `${ch.basis} · 저장된 기록으로 자동 계산돼요.`;
    $("#challenge-list").replaceChildren(
      ...ch.items.map((c) => h("div", { class: `challenge${c.done ? " done" : ""}`, "aria-label": `${c.title} ${c.done ? "달성" : `${c.current}/${c.target}`}` },
        h("span", { class: "badge-icon", "aria-hidden": "true", text: c.icon }),
        h("div", { style: "min-width:0" },
          h("strong", { text: c.title }),
          h("small", { text: `${c.period} · ${c.detail}` }),
          h("div", { class: "challenge-bar" }, h("span", { style: `width:${Math.round((c.current / c.target) * 100)}%` })),
          h("div", { class: "challenge-count", text: c.done ? "달성 ✓" : `${c.current} / ${c.target}` }),
        ),
      )),
    );
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

  // "오늘의 운동 목표": today's part of the weekly routine, one row per exercise.
  function renderTodayGoal(tw) {
    const box = $("#today-workout");
    const isWorkout = Boolean(tw && tw.status === "workout");
    const exercises = isWorkout ? tw.session.exercises : [];
    const checked = new Set(isWorkout ? tw.checked || [] : []);
    const done = exercises.filter((ex) => checked.has(ex.key)).length;
    const total = exercises.length;
    const pct = total ? Math.round((done / total) * 100) : 0; // today's goal completion rate

    // progress bar in the card + ring on the right
    $("#goal-progress-count").hidden = !isWorkout;
    $("#goal-progress").hidden = !isWorkout;
    $("#completed-count").textContent = String(done);
    $("#total-count").textContent = String(total);
    $("#goal-pct").textContent = `${pct}%`;
    $(".progress-fill").style.width = `${pct}%`;
    $("#goal-progress").setAttribute("aria-valuemax", String(total));
    $("#goal-progress").setAttribute("aria-valuenow", String(done));
    const ring = $("#today-ring");
    ring.style.setProperty("--progress", `${pct}%`);
    $("#ring-count").textContent = isWorkout ? `${pct}%` : tw && tw.status === "rest" ? "쉼" : "–";
    ring.setAttribute("aria-label", isWorkout ? `오늘 운동 목표 ${pct}% 달성 (${total}개 중 ${done}개 완료)` : "오늘 운동 없음");
    if (isWorkout) {
      $("#rhythm-title").textContent = done === total ? "오늘 운동을 모두 마쳤어요" : done ? `오늘 목표 ${pct}% 달성` : "가볍게 시작해 봐요";
      $("#rhythm-copy").textContent = done ? `운동 ${total}개 중 ${done}개를 완료했어요. 나만의 속도로 좋아요.` : "오늘의 운동을 하나씩 채워 봐요.";
    } else if (tw && tw.status === "rest") {
      $("#rhythm-title").textContent = "오늘은 쉬는 날";
      $("#rhythm-copy").textContent = "충분히 쉬는 것도 운동의 일부예요.";
    } else {
      $("#rhythm-title").textContent = "나만의 속도로 좋아요";
      $("#rhythm-copy").textContent = "바디 로그에서 운동 루틴을 받으면 오늘 할 운동이 보여요.";
    }

    renderTodayExtras(tw);
    if (!tw) { box.replaceChildren(); return; }
    const dayLabel = `${WEEKDAYS[tw.weekday]}요일`;
    if (tw.status === "rest") {
      box.replaceChildren(h("div", { class: "tw-rest" }, h("strong", { text: "오늘은 쉬는 날" }), h("small", { text: `${dayLabel}은 휴식 요일이에요. 충분히 쉬는 것도 운동의 일부예요.` })));
      return;
    }
    if (!isWorkout) {
      const text = tw.status === "outdated" ? "주간 루틴을 요일별로 다시 받으면 오늘 할 운동이 여기에 보여요."
        : tw.status === "unscheduled" ? `${dayLabel}이 운동하는 날로 바뀌었어요. 루틴을 다시 받으면 오늘 운동이 편성돼요.`
        : "바디 로그에서 주간 운동 루틴을 받으면 오늘 할 운동이 여기에 목록으로 보여요.";
      box.replaceChildren(h("div", { class: "tw-box" }, h("p", { style: "margin:0;font-size:12px;color:var(--muted)", text }),
        h("div", { class: "notice-actions" }, btn(tw.status === "no_plan" ? "루틴 받으러 가기" : "루틴 다시 받기", "mini-button accent", () => showScreen("body")))));
      return;
    }
    const s = tw.session;
    box.replaceChildren(...[
      h("p", { class: "tw-meta" }, h("strong", { text: `${dayLabel} · ${s.title}` }), [s.focus, `${s.minutes}분`, s.intensity].filter(Boolean).join(" · "), " ", h("span", { class: "tag indigo", text: "AI 추천 루틴" })),
      h("div", { class: "mission-list" }, exercises.map((ex, i) => {
        const isDone = checked.has(ex.key);
        const load = ex.weight_kg ? `${fmt1(ex.weight_kg)}kg${ex.weight_note ? ` · ${ex.weight_note}` : ""}` : ex.weight_note || null;
        const button = btn(isDone ? "완료 ✓" : "완료하기", "mission-action", () => toggleExercise(ex, !isDone, button), { "aria-pressed": String(isDone), "aria-label": `${ex.name} ${isDone ? "완료 취소" : "완료"}` });
        return h("article", { class: `mission${isDone ? " is-done" : ""}` },
          h("span", { class: "mission-icon", "aria-hidden": "true", text: String(i + 1) }),
          h("span", { class: "mission-info" },
            h("strong", null, `${ex.name}${ex.amount ? ` · ${ex.amount}` : ""}`, load ? h("span", { class: ex.weight_kg ? "load-chip" : "load-chip light", text: load }) : null),
            ex.tip ? h("small", { text: ex.tip }) : null),
          button);
      })),
      tw.stale && tw.review && tw.review.status !== "running" ? h("p", { class: "hint", style: "margin:10px 0 0", text: "몸 상태나 운동 설정이 바뀌었는데 아직 루틴에 반영되지 않았어요. 바디 로그에서 루틴을 다시 받을 수 있어요." }) : null,
    ].filter(Boolean)); // replaceChildren would print a null as the text "null"
  }

  // ------------------------------------------------------------------ workout feedback + automatic AI review
  const FEEDBACK_PRESETS = ["너무 쉬웠어요", "힘들어서 못 했어요", "약속 때문에 쉬었어요", "내일은 20분만 가능해요"];
  const DECISION_TEXT = { keep: "계획 유지", increase: "강도·양 늘림", decrease: "강도·양 줄임", reschedule: "일정 변경", mixed: "일부 조정" };
  const TRIGGER_TEXT = { feedback: "피드백 저장", settings: "개인 설정 변경", new_week: "새 주 준비" };

  function adjustmentBox(adj) {
    if (!adj) return null;
    const list = (items) => h("ul", null, (items || []).map((t) => h("li", { text: t })));
    if (adj.source === "agent") {
      const days = (adj.changed_days || []).map((d) => WEEKDAYS[d]).join("·");
      return h("div", { class: "notice info adj-box", style: "margin-top:10px" },
        h("strong", { text: `AI 자동 조정 · ${DECISION_TEXT[adj.decision] || adj.decision} · ${days ? `바뀐 요일 ${days}` : "바뀐 요일 없음"}` }),
        list(adj.summary),
        h("p", { class: "hint", style: "margin:6px 0 0", text: `${TRIGGER_TEXT[adj.trigger] || "자동"} 후 ${fmtStamp(adj.at)} 반영 · 지난 요일과 이미 완료한 운동은 바꾸지 않아요.` }));
    }
    if (adj.source === "regenerate") return h("div", { class: "notice info adj-box", style: "margin-top:10px" }, h("strong", { text: `루틴 다시 받기 · ${fmtStamp(adj.at)}` }), list(adj.summary));
    if (adj.rate != null) return h("div", { class: "notice info", style: "margin-top:10px" }, `지난주 달성률 ${Math.round(adj.rate * 100)}% (${adj.planned}개 중 ${adj.done}개 완료)를 반영했던 이전 방식의 조정이에요.`);
    return null;
  }

  function reviewNotice(review) {
    if (!review) return null;
    if (review.status === "running") {
      return h("div", { class: "notice info", style: "margin-top:10px" }, spinner(), ` AI가 최신 정보와 기록으로 ${review.trigger === "new_week" ? "이번 주 루틴을 준비" : "남은 루틴을 점검"}하고 있어요…`);
    }
    if (review.status === "failed") return h("div", { class: "notice", style: "margin-top:10px" }, h("strong", { text: "자동 조정 실패 · " }), review.message);
    if (review.status === "up_to_date") return h("p", { class: "hint", style: "margin:10px 0 0", text: review.message });
    return null;
  }

  // While the AI review runs (in the background on the server), refresh every few seconds.
  let reviewTimer = null;
  let reviewPolls = 0;
  function watchReview(...reviews) {
    if (!reviews.some((r) => r && r.status === "running")) { reviewPolls = 0; return; }
    if (reviewTimer || reviewPolls >= 60) return;
    const epoch = state.epoch;
    reviewTimer = setTimeout(() => {
      reviewTimer = null;
      reviewPolls += 1;
      if (epoch !== state.epoch) return;
      loadOverview();
      if ($("#screen-body").classList.contains("active")) loadWorkout();
    }, 3000);
  }

  function renderTodayExtras(tw) {
    const reviewBox = $("#today-review");
    const fbBox = $("#today-feedback");
    const hasPlan = Boolean(tw && tw.status !== "no_plan" && tw.review && tw.review.status !== "none");
    if (!hasPlan) { reviewBox.replaceChildren(); fbBox.replaceChildren(); return; }
    const latest = tw.latest_adjustment;
    fill(reviewBox, reviewNotice(tw.review), tw.review.status !== "running" ? adjustmentBox(latest) : null);
    watchReview(tw.review);
    const area = $("#workout-feedback");
    if (area && document.activeElement === area) return; // don't wipe what the user is typing
    fbBox.replaceChildren(feedbackBox(tw));
  }

  function feedbackBox(tw) {
    const saved = tw.feedback && tw.feedback.date === todayStr() ? tw.feedback : null;
    const area = h("textarea", { id: "workout-feedback", maxlength: "200", rows: "2", "aria-label": "오늘 운동 피드백",
      placeholder: "선택 사항이에요. 비워 두고 저장하면 ‘피드백 없음’으로 기록돼요.",
      oninput: (e) => { state.feedbackDraft = e.currentTarget.value; } });
    area.value = state.feedbackDraft != null ? state.feedbackDraft : saved ? saved.text : "";
    const chips = FEEDBACK_PRESETS.map((t) => btn(t, "mini-button", () => { area.value = t; state.feedbackDraft = t; area.focus(); }));
    const save = btn("피드백 저장", "secondary-button small-button", () => saveFeedback(area.value, save));
    return h("div", { class: "tw-feedback" },
      h("label", { class: "tw-feedback-title", for: "workout-feedback", text: "오늘 운동은 어떠셨나요? (선택)" }),
      h("div", { class: "chip-row" }, chips),
      area,
      h("div", { class: "tw-feedback-foot" }, save,
        h("span", { class: "hint", text: saved ? `저장됨 · ${saved.label} · ${fmtStamp(saved.updated_at)}` : "입력하지 않아도 모든 기능을 그대로 쓸 수 있어요." })));
  }

  async function saveFeedback(value, button) {
    const epoch = state.epoch;
    await withBusy(button, "저장 중", async () => {
      try {
        const res = await api("/api/workout-feedback", { method: "PUT", body: { date: todayStr(), text: value.trim() } });
        if (epoch !== state.epoch) return;
        state.feedbackDraft = null;
        $("#today-feedback").replaceChildren(); // re-rendered below with the saved value
        notify(!res.changed ? "같은 피드백이 이미 저장돼 있어요." : res.review_started ? "피드백을 저장했어요. AI가 남은 루틴을 점검해요." : "피드백을 저장했어요.");
      } catch (error) {
        notify(error.message);
      }
    });
    if (epoch !== state.epoch) return;
    loadOverview();
    if ($("#screen-body").classList.contains("active")) loadWorkout();
  }

  async function toggleExercise(ex, done, button) {
    const ov = state.overview;
    if (!ov) return;
    const epoch = state.epoch;
    await withBusy(button, "저장 중", async () => {
      try {
        const res = await api("/api/exercise-checks", { method: "PUT", body: { date: ov.today, item: ex.key, done } });
        if (epoch !== state.epoch || !state.overview || !state.overview.today_workout) return;
        state.overview.today_workout.checked = res.checked;
        notify(done ? `${ex.name} 완료! 잘했어요.` : "완료 표시를 취소했어요.");
      } catch (error) {
        notify(error.message);
      }
    });
    if (epoch === state.epoch) {
      renderTodayGoal(state.overview && state.overview.today_workout);
      loadOverview(); // streak, week dots and challenges count this as a movement day
    }
  }

  function settingsAreEmpty(s) {
    if (!s) return true;
    const targets = s.targets || {};
    const body = s.body || {};
    return !s.goal_type && !s.experience && !(s.allergies || []).length && !(s.avoid_foods || []).length && !s.food_preferences && body.height_cm == null && body.weight_kg == null && s.age == null;
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
    const t = (state.nutrition && state.nutrition.current) || {};
    const targetText = NUTRIENTS.filter(([k]) => t[k] != null).map(([k, label, unit]) => `${label} ${fmtNum(t[k])}${unit}`).join(" · ");
    const SEX = { male: "남성", female: "여성" };
    const ACT = { sedentary: "주로 앉아서 지내요", light: "가볍게 자주 움직여요", active: "몸을 많이 쓰는 일을 해요" };
    const SPECIAL = { none: "해당 없음", pregnant: "임신 또는 수유 중", medical: "의료진의 식이 관리 중" };
    const rows = [
      ["목표", goal || "설정 안 함"],
      ["현재 몸 상태", bodyText(s.body) || "설정 안 함"],
      ["운동 경험", s.experience || "설정 안 함"],
      ["사용 가능한 장비", [...(s.equipment || []), s.equipment_note].filter(Boolean).join(", ") || "설정 안 함"],
      ["운동 계획", [
        s.rest_days != null ? (s.rest_days.length ? `휴식 ${s.rest_days.map((d) => WEEKDAYS[d]).join("·")}` : "휴식 요일 없음") : null,
        s.workouts_per_week != null ? `주 ${s.workouts_per_week}회` : null,
        s.minutes_per_session != null ? `${s.minutes_per_session}분` : null,
      ].filter(Boolean).join(" · ") || "설정 안 함"],
      ["식사 선호", s.food_preferences || "설정 안 함"],
      ["알레르기", (s.allergies || []).join(", ") || "없음"],
      ["피하고 싶은 음식", (s.avoid_foods || []).join(", ") || "없음"],
      ["계산용 정보", [s.age != null ? `만 ${s.age}세` : null, SEX[s.sex], ACT[s.activity_level], SPECIAL[s.special_condition]].filter(Boolean).join(" · ") || "설정 안 함"],
      ["하루 영양 목표 (자동)", targetText || "계산 전 (달성률 미표시)"],
      ["시간대", userTz()],
    ];
    $("#settings-summary").replaceChildren(...rows.map(([label, value]) => h("div", null, h("span", { text: label }), value)));
    renderNutritionCard($("#nutrition-panel"));
    renderTodayGoal(state.overview ? state.overview.today_workout : null);
  }

  function bodyText(body) {
    const b = body || {};
    return [
      b.height_cm != null ? `키 ${fmt1(b.height_cm)}cm` : null,
      b.weight_kg != null ? `몸무게 ${fmt1(b.weight_kg)}kg` : null,
      b.skeletal_muscle_kg != null ? `골격근량 ${fmt1(b.skeletal_muscle_kg)}kg` : null,
    ].filter(Boolean).join(" · ");
  }

  // ------------------------------------------------------------------ nutrition targets (calculated on the server)
  async function loadNutrition() {
    const epoch = state.epoch;
    try {
      const data = await api(`/api/nutrition-target?today=${todayStr()}`);
      if (epoch !== state.epoch) return;
      state.nutrition = data;
      renderProfile();
    } catch (error) {
      if (epoch === state.epoch) $("#nutrition-panel").replaceChildren(errorNotice(error, loadNutrition));
    }
  }

  const GOAL_TEXT = { loss: "감량 목표: 유지 열량에서 줄임", gain: "근육 증가 목표: 유지 열량 + 단백질 높임", maintain: "유지 목표: 유지 열량 그대로" };

  function renderNutritionCard(box, { compact = false } = {}) {
    const n = state.nutrition;
    if (!n) { box.replaceChildren(loadingLine("영양 목표를 불러오는 중이에요…")); return; }
    const cur = n.current;
    const kids = [];
    const setupNotice = () => {
      if (n.setup.status === "ok") return null;
      const text = n.setup.status === "missing" ? `자동 계산에 필요한 정보가 없어요: ${n.setup.missing.join(", ")}` : n.setup.message;
      return h("div", { class: n.setup.status === "missing" ? "notice info" : "notice", style: "margin-top:8px" }, text,
        compact ? null : h("div", { class: "notice-actions" }, btn("개인 설정 수정", "mini-button accent", openNutritionSettings)));
    };
    if (!cur) {
      kids.push(h("p", { class: "hint", style: "margin:0", text: "아직 계산된 목표가 없어 달성률을 표시하지 않아요." }), setupNotice());
      box.replaceChildren(...kids.filter(Boolean));
      return;
    }
    const shares = (cur.basis && cur.basis.shares) || {};
    const stat = (label, value, note) => h("div", { class: "stat-card" }, h("span", { text: label }), h("strong", { text: value }), h("small", { text: note }));
    kids.push(
      h("div", null,
        h("span", { class: "tag sage", text: "개인 설정 기반 추정치" }), " ",
        cur.source === "legacy" ? h("span", { class: "tag", text: "자동 계산 이전에 직접 입력한 값" }) : null),
      h("div", { class: "nt-grid" },
        stat("하루 열량", cur.kcal != null ? `${fmtNum(cur.kcal)}kcal` : "–", "목표"),
        stat("탄수화물", cur.carbs_g != null ? `${fmtNum(cur.carbs_g)}g` : "–", shares.carbs != null ? `열량의 ${shares.carbs}%` : ""),
        stat("단백질", cur.protein_g != null ? `${fmtNum(cur.protein_g)}g` : "–", shares.protein != null ? `열량의 ${shares.protein}%` : ""),
        stat("지방", cur.fat_g != null ? `${fmtNum(cur.fat_g)}g` : "–", shares.fat != null ? `열량의 ${shares.fat}%` : "")),
      h("p", { class: "hint", style: "margin:6px 0 0", text: `마지막 갱신 ${fmtStamp(cur.created_at)} · ${cur.source === "legacy" ? "자동 계산 전 날짜" : `${fmtDate(cur.effective_date, { year: "numeric", month: "long", day: "numeric" })}부터`} 적용` }),
    );
    kids.push(setupNotice());
    const b = cur.basis;
    if (!compact && b) {
      kids.push(h("p", { class: "sub-title", text: "계산 근거 요약" }), h("ul", { class: "nt-basis" },
        h("li", { text: `안정 시 에너지(Mifflin-St Jeor 식): ${fmtNum(b.rmr)}kcal — 키·몸무게·나이·성별로 계산` }),
        h("li", { text: `평소 활동(운동 제외) ‘${b.activity_label}’ × ${b.pal} = ${fmtNum(b.daily_life)}kcal (${b.pal_note})` }),
        h("li", { text: `계획한 운동 주 ${b.workout_days}회 × ${b.workout_minutes}분, ${b.exercise_met} MET → 하루 평균 +${fmtNum(b.exercise)}kcal (한 번만 더해요)` }),
        h("li", { text: `유지 열량 ${fmtNum(b.maintenance)}kcal → 목표 ${fmtNum(cur.kcal)}kcal · ${GOAL_TEXT[b.goal_kind] || ""}` }),
        h("li", { text: `단백질 체중 1kg당 ${b.protein_per_kg}g, 지방은 열량의 약 ${b.shares.fat}%, 나머지는 탄수화물 (모두 권장 비율 범위 안)` }),
        h("li", { text: "골격근량은 제지방량과 다른 값이라 계산에 쓰지 않아요." }),
        ...(b.notes || []).map((t) => h("li", { text: t })),
      ));
      // AI explanation (words only; the numbers above come from the server calculation)
      if (cur.source === "auto") {
        const exp = cur.explanation;
        kids.push(h("p", { class: "sub-title" }, "쉽게 풀어 쓴 설명 ", h("span", { class: "tag indigo", text: "AI 작성 · 숫자는 서버 계산값" })));
        if (state.explaining) kids.push(loadingLine("AI가 설명을 쓰는 중이에요…"));
        else if (exp && cur.explanation_status === "ok") {
          kids.push(h("p", { style: "margin:8px 0 0;font-size:12.5px;line-height:1.65", text: exp.summary }));
          if (exp.notes.length) kids.push(h("ul", { class: "coach-list" }, exp.notes.map((t) => h("li", { text: t }))));
          if (exp.caution) kids.push(h("div", { class: "notice", style: "margin-top:8px" }, exp.caution));
        } else if (!state.config.ai_configured) {
          kids.push(h("p", { class: "hint", text: "AI가 설정되지 않아 설명은 없어요. 위 계산 결과는 그대로 쓸 수 있어요." }));
        } else {
          if (state.explainError || cur.explanation_status === "failed") kids.push(h("div", { class: "notice", style: "margin-top:8px" }, `${state.explainError ? state.explainError.message : "설명을 만들지 못했어요."} 계산된 목표는 그대로 쓸 수 있어요.`));
          kids.push(btn(cur.explanation_status === "failed" || state.explainError ? "설명 다시 받기" : "AI 설명 받기", "mini-button", requestExplanation, { style: "margin-top:8px" }));
        }
      }
      kids.push(h("details", { style: "margin-top:12px;font-size:11px;color:var(--muted)" }, h("summary", { text: "계산식·기준 출처와 적용 범위" }),
        h("ul", { class: "nt-history" }, (n.sources || []).map((s) => h("li", { text: s.title }))),
        h("p", { style: "margin:6px 0 0", text: "적용 범위: 만 19~78세 성인. 임신·수유 중이거나 의료진의 식이 관리를 받는 경우, 저체중(BMI 18.5 미만)에서 감량하려는 경우에는 목표를 만들지 않아요. 계산값은 추정치라 실제 필요량과 다를 수 있어요." })));
      const older = (n.history || []).filter((r) => r.id !== cur.id && r.status === "ok").slice(0, 4);
      if (older.length) kids.push(h("p", { class: "sub-title", text: "이전 목표 (그 날짜들의 달성률에 그대로 쓰여요)" }), h("ul", { class: "nt-history" },
        older.map((r) => h("li", { text: `${r.source === "legacy" ? "직접 입력" : `${fmtDate(r.effective_date)}부터`} · ${fmtNum(r.kcal || 0)}kcal · 탄 ${fmtNum(r.carbs_g || 0)}g · 단 ${fmtNum(r.protein_g || 0)}g · 지 ${fmtNum(r.fat_g || 0)}g` }))));
    }
    box.replaceChildren(...kids.filter(Boolean));
  }

  const openNutritionSettings = () => openSettings({ focus: "#set-age", section: "#set-body-section" });

  async function requestExplanation() {
    if (state.explaining || !state.config.ai_configured) return;
    const epoch = state.epoch;
    state.explaining = true;
    state.explainError = null;
    renderProfile();
    try {
      const data = await api("/api/nutrition-target/explain", { method: "POST", body: { today: todayStr() }, timeoutMs: AI_TIMEOUT_MS });
      if (epoch !== state.epoch) return;
      state.nutrition = data;
    } catch (error) {
      if (epoch !== state.epoch) return;
      state.explainError = error;
    }
    state.explaining = false;
    renderProfile();
  }

  function renderTodayNutrition(n) {
    const box = $("#today-nutrition");
    if (!n) { box.replaceChildren(); return; }
    if (!n.current) {
      $("#today-nutrition-sub").textContent = "개인 설정을 입력하면 자동으로 계산돼요";
      box.replaceChildren(h("p", { class: "hint", style: "margin:0", text: n.setup.status === "missing" ? `필요한 정보: ${n.setup.missing.join(", ")}` : n.setup.message || "아직 계산된 목표가 없어요." }),
        h("div", { class: "notice-actions" }, btn("개인 설정 입력", "mini-button accent", openNutritionSettings)));
      return;
    }
    $("#today-nutrition-sub").textContent = `개인 설정 기반 추정치 · ${n.current.source === "legacy" ? "직접 입력한 값" : `${fmtDate(n.current.effective_date)}부터 적용`}`;
    fill(box,
      h("div", { class: "nt-rows" }, NUTRIENTS.map(([key, label, unit]) => {
        const p = n.progress[key];
        const pct = p.pct == null ? 0 : p.pct;
        return h("div", { class: "nt-row" }, h("span", { text: label }),
          h("div", { class: "challenge-bar" }, h("span", { style: `width:${Math.min(100, pct)}%` })),
          h("span", null, h("b", { text: p.value == null ? "–" : fmtNum(p.value) }), ` / ${p.target == null ? "–" : fmtNum(p.target)}${unit}${p.pct == null ? "" : ` · ${p.pct}%`}`));
      })),
      n.uncounted ? h("p", { class: "hint", style: "margin:6px 0 0", text: `분석되지 않은 기록 ${n.uncounted}개는 합계에서 빠졌어요.` }) : null,
    );
  }

  // ------------------------------------------------------------------ settings form
  function openSettings({ focus = null, section = "#set-body-section" } = {}) {
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
    // Rest days: an untouched, never-set picker is not sent, so saving other settings doesn't turn every day into a workout day.
    state.restDirty = false;
    $("#set-rest-days").replaceChildren(
      ...WEEKDAYS.map((label, idx) => h("label", { class: "chip-option rest-chip" },
        h("input", { type: "checkbox", value: String(idx), checked: (s.rest_days || []).includes(idx), onchange: () => { state.restDirty = true; updateRestHint(); } }),
        h("span", { text: label }))),
    );
    updateRestHint();
    $("#set-minutes").value = s.minutes_per_session ?? "";
    $("#set-pref").value = s.food_preferences || "";
    $("#set-allergies").value = (s.allergies || []).join(", ");
    $("#set-avoid").value = (s.avoid_foods || []).join(", ");
    $("#set-age").value = s.age ?? "";
    $("#set-sex").value = s.sex || "";
    $("#set-activity").value = s.activity_level || "";
    $("#set-special").value = s.special_condition || "";
    renderNutritionCard($("#set-nutrition-card"), { compact: true });
    $("#set-timezone").textContent = `기록 날짜는 ${userTz()} 시간대 기준으로 계산해요.`;
    showFormError("#settings-form-error", "");
    openDialog($("#settings-dialog"));
    if (focus) {
      $(section).scrollIntoView({ block: "start" });
      $(focus).focus({ preventScroll: true });
    }
  }

  const selectedRestDays = () => $$("#set-rest-days input:checked").map((i) => Number(i.value));
  function updateRestHint() {
    const s = state.settings || {};
    const rest = selectedRestDays();
    const hint = $("#set-rest-hint");
    if (!state.restDirty && s.rest_days == null) {
      hint.textContent = `아직 정하지 않았어요. 쉬는 요일을 고르면 나머지 요일에 맞춰 주간 운동 루틴을 편성해요.${s.workouts_per_week ? ` (지금은 주 ${s.workouts_per_week}회 기준으로 고르게 배치)` : ""}`;
    } else if (rest.length === 7) {
      hint.textContent = "운동할 요일이 하루 이상 있어야 해요.";
    } else {
      hint.textContent = rest.length ? `${rest.map((d) => WEEKDAYS[d]).join("·")} 쉬고, 주 ${7 - rest.length}회 운동해요.` : "쉬는 요일 없이 매일 운동해요. 가벼운 날을 섞는 걸 추천해요.";
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
      minutes_per_session: numOrNull("#set-minutes"),
      food_preferences: $("#set-pref").value,
      allergies: $("#set-allergies").value,
      avoid_foods: $("#set-avoid").value,
      age: numOrNull("#set-age"),
      sex: $("#set-sex").value,
      activity_level: $("#set-activity").value,
      special_condition: $("#set-special").value,
      today: todayStr(), // a recalculated target takes effect from the user's today
      body: { height_cm: numOrNull("#set-height"), weight_kg: numOrNull("#set-weight"), skeletal_muscle_kg: numOrNull("#set-muscle"), condition_note: $("#set-condition").value },
    };
    if (state.restDirty || state.settings.rest_days != null) payload.rest_days = selectedRestDays();
    await withBusy($("#settings-save"), "저장 중", async () => {
      try {
        const res = await api("/api/settings", { method: "PUT", body: payload });
        if (epoch !== state.epoch) return;
        state.settings = res.settings;
        state.nutrition = res.nutrition;
        closeDialog($("#settings-dialog"));
        renderProfile();
        notify((res.nutrition_changed ? (res.nutrition.current ? "개인 설정을 저장하고 하루 영양 목표를 다시 계산했어요." : "개인 설정을 저장했어요. 영양 목표는 필요한 정보가 있어야 계산돼요.") : "개인 설정을 저장했어요.")
          + (res.routine_review_started ? " AI가 바뀐 정보로 운동 루틴도 점검해요." : ""));
        if (res.routine_review_started) loadWorkout();
        if (res.nutrition_changed && res.nutrition.current && res.nutrition.current.source === "auto") requestExplanation();
        if (state.food.date && $("#screen-food").classList.contains("active")) loadFoodDay({ keep: true });
        if ($("#screen-body").classList.contains("active")) loadWorkout();
        loadOverview(); // rest days change today's workout and the challenge targets
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
      kids.push(h("div", { class: "notice info" }, "이 날짜에 적용된 영양 목표가 없어 달성률은 표시하지 않아요. 개인 설정을 입력하면 자동으로 계산돼요.", h("div", { class: "notice-actions" }, btn("개인 설정 수정", "mini-button", openNutritionSettings))));
    } else if (day.target) {
      kids.push(h("p", { class: "hint", style: "margin-top:6px", text: `목표: 개인 설정 기반 추정치 (${day.target.source === "legacy" ? "자동 계산 이전에 직접 입력한 값" : `${fmtDate(day.target.effective_date)}부터 적용`}, 갱신 ${fmtStamp(day.target.created_at)})` }));
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
        q.id === "set_targets" ? btn("개인 설정 수정", "mini-button", openNutritionSettings) :
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
    fill($("#view-dialog-foot"),
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
    fill($("#view-dialog-foot"),
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
  const todayWeekday = () => (utcDate(todayStr()).getUTCDay() + 6) % 7; // Mon=0 ... Sun=6
  const openRestDays = () => openSettings({ focus: "#set-rest-days input", section: "#set-rest-section" });

  function exerciseList(exercises) {
    return h("ol", null, exercises.map((ex) => {
      const load = ex.weight_kg ? `${fmt1(ex.weight_kg)}kg${ex.weight_note ? ` · ${ex.weight_note}` : ""}` : ex.weight_note || null;
      return h("li", null, `${ex.name}${ex.amount ? ` · ${ex.amount}` : ""}`, load ? h("span", { class: ex.weight_kg ? "load-chip" : "load-chip light", text: load }) : null, ex.tip ? h("small", { text: ex.tip }) : null);
    }));
  }
  async function loadWorkout() {
    const w = state.workout;
    const epoch = state.epoch;
    const token = ++w.token;
    w.error = null;
    if (!w.data) renderRoutine();
    try {
      const data = await api(`/api/workout-plan?today=${todayStr()}`); // a new week may adjust the routine (no AI)
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

    kids.push(h("p", { class: "sub-title" }, "이번 주 운동 루틴 (월~일) ", h("span", { class: "tag indigo", text: "AI 추천 · 참고용" })));
    const sched = w.data.schedule;
    const dayNames = (list) => list.map((d) => WEEKDAYS[d]).join("·");
    const schedText = sched.source === "rest_days"
      ? `운동 ${dayNames(sched.training_days)} · 쉬는 날 ${sched.rest_days.length ? dayNames(sched.rest_days) : "없음"}`
      : `휴식 요일을 아직 정하지 않아 주 ${sched.training_days.length}회(${dayNames(sched.training_days)})로 고르게 배치해요. 쉬는 요일을 고르면 그에 맞춰 편성해요.`;
    kids.push(h("div", { class: "notice info", style: "margin-top:8px" }, schedText, h("div", { class: "notice-actions" }, btn("휴식 요일 바꾸기", "mini-button", openRestDays))));
    if (!state.config.ai_configured) {
      kids.push(h("div", { class: "notice", style: "margin-top:8px" }, `AI 분석이 설정되지 않아 운동 루틴을 추천할 수 없어요. 서버에 ${state.config.ai_key_name || "AI API 키"}를 설정하면 사용할 수 있어요.`));
      box.replaceChildren(...kids);
      return;
    }
    if (plan) {
      const review = w.data.review;
      if (plan.stale && review.status !== "running") kids.push(h("div", { class: "notice", style: "margin-top:8px" }, "몸 상태나 운동 설정이 바뀌었는데 아직 루틴에 반영되지 않았어요. ‘루틴 다시 받기’로 새로 받을 수도 있어요."));
      const notice = reviewNotice(review);
      if (notice) kids.push(notice);
      const adj = review.status !== "running" ? adjustmentBox((plan.adjustments || [])[0]) : null;
      if (adj) kids.push(adj);
      kids.push(h("p", { class: "hint", style: "margin-top:8px", text: "오늘 운동 피드백을 저장하거나 몸 상태·운동 설정을 바꾸면, AI가 기록을 확인해 오늘 남은 운동부터 일요일까지 필요한 부분만 자동으로 조정해요. 다음 주 루틴은 이번 주 운동을 마치거나 새 주에 처음 들어오면 자동으로 준비돼요." }));
      if (w.data.next_week && w.data.next_week.ready) {
        kids.push(h("p", { class: "hint", style: "margin-top:4px", text: w.data.next_week.review.status === "running" ? "다음 주 루틴을 AI가 준비하고 있어요…" : `다음 주(${fmtDate(w.data.next_week.week_start)}~) 루틴도 준비됐어요.` }));
      }
      watchReview(review, w.data.next_week && w.data.next_week.review);
      if (plan.summary) kids.push(h("p", { style: "margin:10px 0 0;font-size:13px;line-height:1.65", text: plan.summary }));
      const todayIdx = todayWeekday();
      const sessionCard = (s) => h("div", { class: `session-card${s.weekday === todayIdx ? " today" : ""}` },
        h("h4", null, s.weekday != null ? `${WEEKDAYS[s.weekday]}요일 · ${s.title}` : s.title, s.weekday === todayIdx ? h("span", { class: "tag indigo", style: "margin-left:6px", text: "오늘" }) : null),
        h("p", { class: "session-meta", text: [s.focus, `${s.minutes}분`, s.intensity].filter(Boolean).join(" · ") }),
        (() => { // what was done on days that have started (kept as-is by every adjustment)
          const p = plan.progress && plan.progress[String(s.weekday)];
          return p && s.weekday <= todayIdx ? h("p", { class: "done-line", text: `완료 ${p.done}/${p.planned}` }) : null;
        })(),
        exerciseList(s.exercises),
      );
      if ((plan.format || 1) >= 3) {
        const byDay = new Map(plan.sessions.map((s) => [s.weekday, s]));
        kids.push(h("div", { class: "week-plan" }, WEEKDAYS.map((label, d) => (byDay.has(d)
          ? sessionCard(byDay.get(d))
          : h("div", { class: `session-card rest${d === todayIdx ? " today" : ""}` }, h("div", null, h("strong", { style: "display:block;font-size:13px", text: `${label}요일` }), h("span", { style: "font-size:12px", text: "쉬는 날" })))))));
      } else {
        kids.push(h("div", { class: "session-grid" }, plan.sessions.map(sessionCard)));
      }
      const hasLoads = plan.sessions.some((s) => s.exercises.some((ex) => ex.weight_kg));
      const loadGear = ["덤벨", "케틀벨", "헬스장"];
      if ((plan.format || 1) < 3) {
        kids.push(h("div", { class: "notice info", style: "margin-top:10px" }, "요일별 편성과 운동별 시작 중량(kg)이 추가됐어요. ‘루틴 다시 받기’를 누르면 반영돼요."));
      } else if (hasLoads) {
        kids.push(h("p", { class: "hint", style: "margin-top:8px", text: "중량은 AI가 몸 상태와 운동 경험으로 추정한 시작값이에요. 마지막 2~3회가 힘든 정도면 적당하고, 자세가 무너지면 낮추세요." }));
      } else if (!(state.settings?.equipment || []).some((e) => loadGear.includes(e))) {
        kids.push(h("p", { class: "hint", style: "margin-top:8px", text: "지금 장비로는 맨몸 운동만 추천돼요. 개인 설정의 장비에 덤벨·케틀벨·헬스장을 추가하면 중량 운동과 시작 중량도 함께 추천돼요." }));
      }
      if (plan.dropped_weights) kids.push(h("p", { class: "hint", style: "margin-top:6px", text: `안전 범위를 벗어난 중량 ${plan.dropped_weights}개는 표시하지 않았어요.` }));
      if (plan.cautions.length) kids.push(h("ul", { class: "coach-list" }, plan.cautions.map((c) => h("li", { text: c }))));
      if (plan.filtered_exercises) kids.push(h("p", { class: "hint", style: "margin-top:6px", text: `안전하지 않은 표현이 담긴 동작 ${plan.filtered_exercises}개는 제외했어요.` }));
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
      const data = await api("/api/workout-plan", { method: "POST", body: { today: todayStr() }, timeoutMs: AI_TIMEOUT_MS });
      if (epoch !== state.epoch) return;
      w.data = data;
      w.token += 1; // a slower GET started earlier must not overwrite the new plan
      loadOverview(); // today's workout on the Today tab
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
    else if (action === "edit-rest") openRestDays();
    else if (action === "edit-nutrition") openNutritionSettings();
    else if (action === "install-app") installApp();
    else if (action === "dismiss-install") { try { localStorage.setItem(INSTALL_DISMISSED, "1"); } catch { /* private mode */ } renderInstall(); }
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
  $("#badge-info").addEventListener("click", () => notify("배지는 저장된 기록으로 자동으로 열려요: 첫 기록, 4일 이상 쉬었다가 다시 기록, 한 주에 3일 이상 기록. 도전과제는 운동 경험(입문·초급·중급·상급)에 맞춰 목표가 달라지고, 주간 과제는 매주 월요일, 월간 과제는 매월 1일에 새로 시작해요."));
  $("#privacy-info").addEventListener("click", () => notify(`기록과 사진은 이 서버에 내 계정으로만 저장돼요. AI 분석을 요청할 때만 사진과 식사 정보가 AI 서비스(${(state.config && state.config.ai_provider_name) || "AI 서비스"})로 전송돼요.`));

  // ------------------------------------------------------------------ installable app (PWA)
  const isStandalone = () => window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  let installPrompt = null; // Android/Chrome hand this over when the app can be installed
  const INSTALL_DISMISSED = "hl-install-dismissed";
  const installDismissed = () => { try { return localStorage.getItem(INSTALL_DISMISSED) === "1"; } catch { return false; } };

  if ("serviceWorker" in navigator && window.isSecureContext) {
    window.addEventListener("load", () => { navigator.serviceWorker.register("/sw.js").catch(() => { /* app still works without it */ }); });
  }
  window.addEventListener("beforeinstallprompt", (event) => { event.preventDefault(); installPrompt = event; renderInstall(); });
  window.addEventListener("appinstalled", () => { installPrompt = null; notify("Healthlingo 앱을 설치했어요. 홈 화면에서 열 수 있어요."); renderInstall(); });

  async function installApp() {
    if (installPrompt) {
      const prompt = installPrompt;
      installPrompt = null;
      prompt.prompt();
      try { await prompt.userChoice; } catch { /* closed */ }
      renderInstall();
      return;
    }
    showScreen("profile");
    $("#install-panel").scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function renderInstall() {
    const panel = $("#install-panel");
    const standalone = isStandalone();
    $("#install-banner").hidden = standalone || installDismissed() || !(installPrompt || isIOS());
    $("#install-banner-text").textContent = isIOS() && !installPrompt ? "Safari의 공유 버튼 → ‘홈 화면에 추가’로 설치할 수 있어요." : "홈 화면에 추가하면 앱처럼 바로 열 수 있어요.";
    if (standalone) {
      panel.replaceChildren(h("div", { class: "notice info" }, "지금 설치된 앱으로 실행 중이에요. ✓ 기록·사진·AI 분석은 웹과 똑같이 서버에 저장돼요."));
      return;
    }
    if (installPrompt) {
      panel.replaceChildren(h("p", { class: "hint", style: "margin:0", text: "이 기기에 바로 설치할 수 있어요. 설치하면 홈 화면 아이콘으로 열리고 주소창 없이 전체 화면으로 보여요." }),
        btn("앱 설치하기", "primary-button small-button", installApp, { style: "margin-top:10px" }));
      return;
    }
    const steps = isIOS()
      ? ["Safari로 이 사이트를 열어요 (다른 앱 안의 브라우저에서는 안 돼요)", "아래쪽 <b>공유 버튼</b>(네모에 위쪽 화살표)을 눌러요", "<b>홈 화면에 추가</b> → <b>추가</b>를 눌러요", "홈 화면의 Healthlingo 아이콘으로 열어요. 처음 한 번은 초대 링크를 붙여 넣으라는 화면이 나올 수 있어요 (아이폰 앱은 Safari와 로그인 정보를 따로 보관해요)"]
      : ["Chrome 또는 삼성 인터넷으로 이 사이트를 열어요", "오른쪽 위(또는 아래) <b>메뉴(⋮ / ≡)</b>를 눌러요", "<b>앱 설치</b> 또는 <b>홈 화면에 추가</b>를 눌러요", "홈 화면의 Healthlingo 아이콘으로 열어요"];
    const list = h("ol", { class: "install-steps" });
    list.innerHTML = steps.map((s) => `<li>${s}</li>`).join(""); // fixed text above, no user input
    panel.replaceChildren(h("p", { class: "hint", style: "margin:0", text: isIOS() ? "아이폰·아이패드는 Safari에서 이렇게 설치해요." : "휴대폰에서 이렇게 설치해요. (PC에서는 Chrome 주소창 오른쪽의 설치 아이콘을 눌러도 돼요)" }), list);
  }

  // Offline notice: records live on the server, so nothing can be loaded or saved without a connection.
  function renderOnline() { $("#offline-banner").hidden = navigator.onLine !== false; }
  window.addEventListener("offline", renderOnline);
  window.addEventListener("online", () => {
    renderOnline();
    if (!state.user) return;
    notify("다시 연결됐어요.");
    const active = $(".screen.active");
    if (active) showScreen(active.id.replace("screen-", ""));
  });

  // ------------------------------------------------------------------ boot
  async function boot() {
    renderTodayGoal(null);
    resetComparePreview();
    renderInstall();
    renderOnline();
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
