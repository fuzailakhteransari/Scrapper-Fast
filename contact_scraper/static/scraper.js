const state = {
  upload: null,
  jobId: null,
  pollTimer: null,
  jobStatus: null,
};

const els = Object.fromEntries([
  "uploadForm", "pasteForm", "fileInput", "dropZone", "fileLabel", "fileHint",
  "uploadButton", "websitePaste", "companyPaste", "websitePasteCount",
  "companyPasteCount", "websitePasteButton", "companyPasteButton", "fileInfo",
  "workers", "maxPages", "depthDescription", "cleanPhoneRegion",
  "cleanPhoneFormat", "phoneCountryConfidence", "emailPreference",
  "fastQuality", "includeEvidenceColumns", "enableMxCheck", "cleanOutputNote",
  "phoneOptionsNote", "advancedSummary",
  "brightSettingsForm", "brightSettingsState", "brightSettingsMessage",
  "unlockerZone", "apiKey", "browserUsername", "browserPassword", "proxyHost",
  "proxyPort", "browserConcurrency", "searxngBaseUrl", "useWebUnlocker", "useBrowser",
  "saveBrightSettings", "startButton",
  "pauseButton", "stopButton", "statusBadge", "runMessage", "progressLabel",
  "currentStatus", "etaLabel", "progressTrack", "progressBar",
  "processedMetric", "websitesMetric", "successMetric", "failedMetric", "extractedMetric",
  "completionCard", "completionTitle", "completionText", "downloads",
  "summaryProcessed", "summaryWebsites", "summaryEmails", "summaryPhones", "summarySocials",
  "summaryRecoveredRow", "summaryRecovered", "summaryFailed", "logs",
  "retryPromptCard", "retryPromptText", "retryYesButton", "retryNoButton",
].map((id) => [id, document.getElementById(id)]));

const cleanFieldKindMap = {
  website: "website",
  email: "email",
  phone: "phone",
  facebook: "social",
  instagram: "social",
  linkedin: "social",
  twitter_x: "social",
  youtube: "social",
  tiktok: "social",
  pinterest: "social",
  threads: "social",
  address: "details",
  description: "details",
};

const cleanFieldLabels = {
  website: "Website",
  email: "Email",
  phone: "Phone number",
  facebook: "Facebook",
  instagram: "Instagram",
  linkedin: "LinkedIn",
  twitter_x: "X/Twitter",
  youtube: "YouTube",
  tiktok: "TikTok",
  pinterest: "Pinterest",
  threads: "Threads",
  address: "Address",
  description: "Description",
};

const phoneRegionLabels = {
  AUTO: "auto-detected country",
  AU: "Australia",
  US: "United States",
  IN: "India",
  KR: "South Korea",
};

const phoneFormatLabels = {
  national: "local/national format",
  international: "international format",
  e164: "E.164 format",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

function setBadge(text, stateName = "idle") {
  els.statusBadge.textContent = text;
  els.statusBadge.dataset.state = stateName;
}

function selectedVisibleKinds() {
  return Array.from(document.querySelectorAll('input[name="extractKind"]:checked')).map(el => el.value);
}

function selectedCleanFields() {
  return Array.from(document.querySelectorAll('input[name="cleanField"]:checked')).map(el => el.value);
}

function selectedKinds() {
  const kinds = new Set(selectedVisibleKinds());
  selectedCleanFields().forEach((field) => {
    const kind = cleanFieldKindMap[field];
    if (!kind) return;
    if (kind === "details") {
      kinds.add("details");
    } else {
      kinds.add(kind);
    }
  });
  return Array.from(kinds);
}

function selectedCrawlMode() {
  return document.querySelector('input[name="crawlMode"]:checked')?.value || "fast";
}

function syncCrawlMode() {
  const fast = selectedCrawlMode() === "fast";
  els.depthDescription.innerHTML = fast
    ? "<strong>Fast:</strong> stops after the selected clean fields meet the chosen quality target."
    : "<strong>Full Scan:</strong> checks high-value pages and follows relevant internal links until the page limit is reached.";
}

function syncInputType() {
  const inputType = document.querySelector('input[name="inputType"]:checked')?.value || "csv";
  els.uploadForm.hidden = inputType !== "csv";
  els.pasteForm.hidden = inputType !== "paste";
}

function setRunControls(status) {
  state.jobStatus = status;
  const active = ["queued", "running", "paused", "stopping"].includes(status);
  const kinds = selectedKinds();
  const cleanFields = selectedCleanFields();
  els.startButton.disabled = active || !state.upload || kinds.length === 0 || cleanFields.length === 0;
  els.pauseButton.disabled = !["running", "paused"].includes(status);
  els.pauseButton.textContent = status === "paused" ? "Resume" : "Pause";
  els.stopButton.disabled = !["running", "paused"].includes(status);
}

function syncCleanOutput() {
  const fields = selectedCleanFields();
  const phoneSelected = fields.includes("phone");
  els.cleanPhoneRegion.disabled = !phoneSelected;
  els.cleanPhoneFormat.disabled = !phoneSelected;
  els.phoneCountryConfidence.disabled = !phoneSelected;
  const fieldNames = fields.map((field) => cleanFieldLabels[field] || field);
  const shownFields = fieldNames.length > 5
    ? `${fieldNames.slice(0, 5).join(", ")} and ${fieldNames.length - 5} more`
    : fieldNames.join(", ");
  const phoneRegion = phoneRegionLabels[els.cleanPhoneRegion.value] || els.cleanPhoneRegion.value;
  const phoneFormat = phoneFormatLabels[els.cleanPhoneFormat.value] || els.cleanPhoneFormat.value;
  els.phoneOptionsNote.textContent = phoneSelected
    ? `Phone will be cleaned for ${phoneRegion} in ${phoneFormat}.`
    : "Enable Phone number in Columns to Keep to use this.";
  els.advancedSummary.textContent = `${els.fastQuality.value} quality, ${els.emailPreference.value} email, ${els.phoneCountryConfidence.value} phone country`;
  els.cleanOutputNote.textContent = fields.length
    ? `Clean file will include: ${shownFields}.${phoneSelected ? ` Phone: ${phoneRegion}, ${phoneFormat}.` : ""}${els.includeEvidenceColumns.checked ? " Evidence columns will also be added." : ""}`
    : "Select at least one clean CSV column.";
  setRunControls(state.jobStatus || "idle");
}

function chooseFile() {
  const file = els.fileInput.files?.[0];
  if (!file) {
    els.fileLabel.textContent = "Choose CSV file";
    els.fileHint.textContent = "Website column, or Company with optional Location";
    els.dropZone.classList.remove("ready");
    return;
  }
  els.fileLabel.textContent = file.name;
  els.fileHint.textContent = `${Math.max(1, Math.round(file.size / 1024))} KB selected`;
  els.dropZone.classList.add("ready");
}

async function uploadFile(event) {
  event.preventDefault();
  const file = els.fileInput.files?.[0];
  if (!file) {
    els.fileInfo.className = "file-info error";
    els.fileInfo.textContent = "Choose a CSV file first.";
    return;
  }
  els.uploadButton.disabled = true;
  els.fileInfo.className = "file-info muted";
  els.fileInfo.textContent = "Reading CSV...";
  const body = new FormData();
  body.append("file", file);
  try {
    const response = await fetch("/api/upload", { method: "POST", body });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Upload failed.");
    state.upload = data;
    const unit = data.inputType === "company" ? "companies" : "websites";
    const detected = data.inputType === "company"
      ? `${data.rowCount} rows, ${data.uniqueCount} unique companies detected`
      : `${data.rowCount} rows, ${data.uniqueCount} unique websites detected`;
    const processNote = data.inputType === "company"
      ? "<br>Company mode detected. Verified websites will be found first, then any selected contact data will be scraped from those websites."
      : "";
    els.fileInfo.className = "file-info ok";
    els.fileInfo.innerHTML = `<strong>${escapeHtml(data.originalName)}</strong><br>` +
      detected +
      (data.invalidCount ? `, ${data.invalidCount} invalid` : "") +
      processNote;
    els.runMessage.textContent = data.inputType === "company"
      ? `Ready: ${data.uniqueCount} companies, websites will be found first`
      : `Ready: ${data.uniqueCount} ${unit}`;
    setBadge("Ready", "idle");
    setRunControls("idle");
  } catch (error) {
    state.upload = null;
    els.fileInfo.className = "file-info error";
    els.fileInfo.textContent = error.message;
    setBadge("Upload error", "failed");
  } finally {
    els.uploadButton.disabled = false;
  }
}

function pastedWebsiteEntries() {
  return els.websitePaste.value
    .split(/\r?\n/)
    .flatMap((line) => line.split(/[,\t]/))
    .map((value) => value.trim())
    .filter(Boolean);
}

function pastedCompanyEntries() {
  return els.companyPaste.value
    .split(/\r?\n/)
    .map((value) => value.trim())
    .filter(Boolean);
}

function updateWebsitePasteCount() {
  const count = pastedWebsiteEntries().length;
  els.websitePasteCount.textContent = `${count} ${count === 1 ? "entry" : "entries"}`;
}

function updateCompanyPasteCount() {
  const count = pastedCompanyEntries().length;
  els.companyPasteCount.textContent = `${count} ${count === 1 ? "entry" : "entries"}`;
}

async function submitPastedInput(mode) {
  const textarea = mode === "website" ? els.websitePaste : els.companyPaste;
  const button = mode === "website" ? els.websitePasteButton : els.companyPasteButton;
  const pastedInput = textarea.value.trim();
  if (!pastedInput) {
    els.fileInfo.className = "file-info error";
    els.fileInfo.textContent = mode === "website"
      ? "Paste at least one website."
      : "Paste at least one company name.";
    return;
  }
  button.disabled = true;
  els.fileInfo.className = "file-info muted";
  els.fileInfo.textContent = mode === "website"
    ? "Checking pasted websites..."
    : "Checking pasted companies...";
  try {
    const response = await fetch("/api/paste", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ websites: pastedInput, mode }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not use pasted input.");
    state.upload = data;
    const detected = data.inputType === "company"
      ? `${data.rowCount} entries, ${data.uniqueCount} unique companies detected`
      : `${data.rowCount} entries, ${data.uniqueCount} unique websites detected`;
    const processNote = data.inputType === "company"
      ? "<br>Company mode detected. Verified websites will be found first, then any selected contact data will be scraped from those websites."
      : "";
    els.fileInfo.className = "file-info ok";
    els.fileInfo.innerHTML = `<strong>${data.inputType === "company" ? "Pasted company list" : "Pasted website list"}</strong><br>` +
      detected +
      (data.invalidCount ? `, ${data.invalidCount} invalid` : "") +
      processNote;
    els.runMessage.textContent = data.inputType === "company"
      ? `Ready: ${data.uniqueCount} companies, websites will be found first`
      : `Ready: ${data.uniqueCount} websites`;
    setBadge("Ready", "idle");
    setRunControls("idle");
  } catch (error) {
    state.upload = null;
    els.fileInfo.className = "file-info error";
    els.fileInfo.textContent = error.message;
    setBadge("Input error", "failed");
    setRunControls("idle");
  } finally {
    button.disabled = false;
  }
}

function resetRunView() {
  els.completionCard.hidden = true;
  els.retryPromptCard.hidden = true;
  els.progressBar.style.width = "0%";
  els.progressTrack.setAttribute("aria-valuenow", "0");
  els.progressLabel.textContent = "0%";
  els.currentStatus.textContent = "Starting...";
  els.etaLabel.textContent = "ETA --";
  els.processedMetric.textContent = `0 / ${state.upload?.uniqueCount || 0}`;
  els.websitesMetric.textContent = "0";
  els.successMetric.textContent = "0";
  els.failedMetric.textContent = "0";
  els.extractedMetric.textContent = "0";
  els.logs.textContent = "Starting scraper...";
}

async function startJob() {
  if (!state.upload) return;
  const cleanFields = selectedCleanFields();
  if (!cleanFields.length) {
    els.currentStatus.textContent = "Select at least one clean CSV field.";
    setRunControls(state.jobStatus || "idle");
    return;
  }
  resetRunView();
  setRunControls("queued");
  setBadge("Starting", "running");
  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        uploadId: state.upload.id,
        selectedKinds: selectedKinds(),
        cleanFields,
        cleanPhoneRegion: els.cleanPhoneRegion.value,
        cleanPhoneFormat: els.cleanPhoneFormat.value,
        phoneCountryConfidence: els.phoneCountryConfidence.value,
        emailPreference: els.emailPreference.value,
        fastQuality: els.fastQuality.value,
        includeEvidenceColumns: els.includeEvidenceColumns.checked,
        enableMxCheck: els.enableMxCheck.checked,
        crawlMode: selectedCrawlMode(),
        maxPages: Number(els.maxPages.value || 6),
        workers: Number(els.workers.value || 40),
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not start.");
    state.jobId = data.id;
    renderJob(data);
    startPolling();
  } catch (error) {
    setBadge("Failed", "failed");
    els.currentStatus.textContent = error.message;
    setRunControls("failed");
  }
}

function populateBrightSettings(data) {
  els.unlockerZone.value = data.unlockerZone || "";
  els.browserUsername.value = data.browserUsername || "";
  els.proxyHost.value = data.proxyHost || "brd.superproxy.io";
  els.proxyPort.value = data.proxyPort || 9222;
  els.browserConcurrency.value = data.browserConcurrency || 3;
  els.searxngBaseUrl.value = data.searxngBaseUrl || "";
  els.useWebUnlocker.checked = Boolean(data.useWebUnlocker);
  els.useBrowser.checked = Boolean(data.useBrowser);
  els.apiKey.value = "";
  els.browserPassword.value = "";
  const unlocker = data.apiKeyConfigured ? "Unlocker configured" : "Unlocker key missing";
  const browser = data.passwordConfigured ? "Browser configured" : "Browser password missing";
  els.brightSettingsState.textContent = `${unlocker} | ${browser}`;
}

async function loadBrightSettings() {
  try {
    const response = await fetch("/api/settings/brightdata");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load settings.");
    populateBrightSettings(data);
  } catch (error) {
    els.brightSettingsState.textContent = "Settings unavailable";
    els.brightSettingsMessage.className = "settings-message error";
    els.brightSettingsMessage.textContent = error.message;
  }
}

async function saveBrightSettings(event) {
  event.preventDefault();
  els.saveBrightSettings.disabled = true;
  els.brightSettingsMessage.className = "settings-message muted";
  els.brightSettingsMessage.textContent = "Saving locally...";
  try {
    const response = await fetch("/api/settings/brightdata", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        unlockerZone: els.unlockerZone.value.trim(),
        apiKey: els.apiKey.value,
        browserUsername: els.browserUsername.value.trim(),
        browserPassword: els.browserPassword.value,
        proxyHost: els.proxyHost.value.trim(),
        proxyPort: Number(els.proxyPort.value),
        browserConcurrency: Number(els.browserConcurrency.value),
        searxngBaseUrl: els.searxngBaseUrl.value.trim(),
        useWebUnlocker: els.useWebUnlocker.checked,
        useBrowser: els.useBrowser.checked,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not save settings.");
    populateBrightSettings(data);
    els.brightSettingsMessage.className = "settings-message ok";
    els.brightSettingsMessage.textContent = data.message;
  } catch (error) {
    els.brightSettingsMessage.className = "settings-message error";
    els.brightSettingsMessage.textContent = error.message;
  } finally {
    els.saveBrightSettings.disabled = false;
  }
}

async function jobAction(action) {
  if (!state.jobId) return;
  const response = await fetch(`/api/job/${state.jobId}/${action}`, { method: "POST" });
  const data = await response.json();
  if (!response.ok) {
    els.currentStatus.textContent = data.error || `${action} failed`;
    return;
  }
  renderJob(data);
}

function renderJob(job) {
  const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
  els.progressBar.style.width = `${progress}%`;
  els.progressTrack.setAttribute("aria-valuenow", String(progress));
  els.progressLabel.textContent = `${progress.toFixed(1)}%`;
  const current = job.current ? ` - ${job.current}` : "";
  els.currentStatus.textContent = `${job.message || job.status}${current}`;
  els.etaLabel.textContent = `ETA ${job.eta || "--"}`;
  els.processedMetric.textContent = `${job.completed || 0} / ${job.total || 0}`;
  els.websitesMetric.textContent = job.websites || 0;
  els.successMetric.textContent = job.success || 0;
  els.failedMetric.textContent = job.failedTotal ?? job.failed ?? 0;
  els.extractedMetric.textContent = job.extracted || 0;
  els.logs.textContent = (job.logs || []).join("\n") || "No activity yet.";
  els.logs.scrollTop = els.logs.scrollHeight;
  setBadge(job.status.charAt(0).toUpperCase() + job.status.slice(1), job.status);
  setRunControls(job.status);

  const isTerminal = ["completed", "completed_initial", "stopped", "failed"].includes(job.status);
  if (isTerminal) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    if (job.status !== "completed_initial") {
      els.retryPromptCard.hidden = true;
    }
    
    els.completionCard.hidden = false;
    els.completionTitle.textContent = job.status === "completed"
      ? "Scraping completed"
      : job.status === "completed_initial" ? "Initial run complete"
      : job.status === "stopped" ? "Scraping stopped safely" : "Scraping failed";
    els.completionText.textContent = job.error || job.message;
    if (job.lookupFailed) {
      els.completionText.textContent += ` ${job.lookupFailed} entr${job.lookupFailed === 1 ? "y" : "ies"} still need manual website review.`;
    }
    els.summaryProcessed.textContent = job.completed || 0;
    els.summaryWebsites.textContent = job.websites || 0;
    els.summaryEmails.textContent = job.emails || 0;
    els.summaryPhones.textContent = job.phones || 0;
    els.summarySocials.textContent = job.socials || 0;
    els.summaryFailed.textContent = job.failedTotal ?? job.failed ?? 0;
    
    if (job.recovered > 0) {
      els.summaryRecoveredRow.hidden = false;
      els.summaryRecovered.textContent = job.recovered;
    } else {
      els.summaryRecoveredRow.hidden = true;
    }
    
    els.downloads.innerHTML = (job.downloads || []).map((file) =>
      `<a class="download" href="${escapeHtml(file.url)}">${escapeHtml(file.name)}</a>`
    ).join("");

    if (job.status === "completed_initial") {
      els.retryPromptCard.hidden = false;
      els.retryPromptText.textContent = `Scraped all websites without Bright Data. ${job.failed} failed due to blocks, captchas, or errors. Do you want to run Bright Data to retry failed URLs?`;
    }
  } else {
    els.completionCard.hidden = true;
    els.retryPromptCard.hidden = true;
  }
}

async function fetchJob() {
  if (!state.jobId) return;
  try {
    const response = await fetch(`/api/job/${state.jobId}`);
    if (!response.ok) return;
    renderJob(await response.json());
  } catch {
    els.currentStatus.textContent = "Connection lost. Retrying...";
  }
}

function startPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(fetchJob, 750);
}

function setupDropZone() {
  ["dragenter", "dragover"].forEach((name) => {
    els.dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      els.dropZone.classList.add("dragging");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    els.dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      els.dropZone.classList.remove("dragging");
    });
  });
  els.dropZone.addEventListener("drop", (event) => {
    if (event.dataTransfer?.files?.length) {
      els.fileInput.files = event.dataTransfer.files;
      chooseFile();
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupDropZone();
  els.fileInput.addEventListener("change", chooseFile);
  els.uploadForm.addEventListener("submit", uploadFile);
  els.websitePaste.addEventListener("input", updateWebsitePasteCount);
  els.companyPaste.addEventListener("input", updateCompanyPasteCount);
  els.websitePasteButton.addEventListener("click", () => submitPastedInput("website"));
  els.companyPasteButton.addEventListener("click", () => submitPastedInput("company"));
  els.startButton.addEventListener("click", startJob);
  els.pauseButton.addEventListener("click", () => {
    jobAction(els.pauseButton.textContent === "Resume" ? "resume" : "pause");
  });
  els.stopButton.addEventListener("click", () => jobAction("stop"));
  
  document.querySelectorAll('input[name="extractKind"]').forEach((input) => {
    input.addEventListener("change", () => {
      setRunControls(state.jobStatus || "idle");
    });
  });
  document.querySelectorAll('input[name="cleanField"]').forEach((input) => {
    input.addEventListener("change", syncCleanOutput);
  });
  els.cleanPhoneRegion.addEventListener("change", syncCleanOutput);
  els.cleanPhoneFormat.addEventListener("change", syncCleanOutput);
  els.phoneCountryConfidence.addEventListener("change", syncCleanOutput);
  els.emailPreference.addEventListener("change", syncCleanOutput);
  els.fastQuality.addEventListener("change", syncCleanOutput);
  els.includeEvidenceColumns.addEventListener("change", syncCleanOutput);
  els.enableMxCheck.addEventListener("change", syncCleanOutput);
  document.querySelectorAll('input[name="inputType"]').forEach((input) => {
    input.addEventListener("change", syncInputType);
  });
  document.querySelectorAll('input[name="crawlMode"]').forEach((input) => {
    input.addEventListener("change", syncCrawlMode);
  });
  
  els.brightSettingsForm.addEventListener("submit", saveBrightSettings);
  
  els.retryYesButton.addEventListener("click", async () => {
    if (!state.jobId) return;
    els.retryYesButton.disabled = true;
    els.retryNoButton.disabled = true;
    try {
      const response = await fetch(`/api/job/${state.jobId}/retry_brightdata`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Retry failed");
      els.retryPromptCard.hidden = true;
      renderJob(data);
      startPolling();
    } catch (error) {
      alert(error.message);
    } finally {
      els.retryYesButton.disabled = false;
      els.retryNoButton.disabled = false;
    }
  });

  els.retryNoButton.addEventListener("click", async () => {
    if (!state.jobId) return;
    els.retryYesButton.disabled = true;
    els.retryNoButton.disabled = true;
    try {
      const response = await fetch(`/api/job/${state.jobId}/no_retry`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Operation failed");
      els.retryPromptCard.hidden = true;
      renderJob(data);
    } catch (error) {
      alert(error.message);
    } finally {
      els.retryYesButton.disabled = false;
      els.retryNoButton.disabled = false;
    }
  });

  syncInputType();
  syncCrawlMode();
  syncCleanOutput();
  updateWebsitePasteCount();
  updateCompanyPasteCount();
  loadBrightSettings();
  setRunControls("idle");
});
