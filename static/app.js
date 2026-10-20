const form = document.querySelector("#job-form");
const baseUrl = document.querySelector("#base-url");
const apiKey = document.querySelector("#api-key");
const personSelect = document.querySelector("#person-id");
const personName = document.querySelector("#person-name");
const peopleMessage = document.querySelector("#people-message");
const jobMessage = document.querySelector("#job-message");
const loadPeopleButton = document.querySelector("#load-people");
const createButton = document.querySelector("#create-button");
const modeSelect = document.querySelector("#mode");
const outputSettingsFields = document.querySelector("#output-settings-fields");
const outputStepHint = document.querySelector("#output-step-hint");
const resultPanel = document.querySelector("#result-panel");
const resultPlaceholder = document.querySelector("#result-placeholder");
const outputLogWrap = document.querySelector("#output-log-wrap");
const resultTitle = document.querySelector("#result-title");
const statusText = document.querySelector("#job-status");
const logView = document.querySelector("#job-log");
const fetchLogView = document.querySelector("#fetch-log");
const imageWrap = document.querySelector("#image-wrap");
const resultImage = document.querySelector("#result-image");
const downloadLink = document.querySelector("#download-link");
const fetchStatus = document.querySelector("#fetch-status");
const fetchProgressWrap = document.querySelector("#fetch-progress-wrap");
const fetchProgress = document.querySelector("#fetch-progress");
const fetchProgressLabel = document.querySelector("#fetch-progress-label");
const outputProgressWrap = document.querySelector("#output-progress-wrap");
const outputProgress = document.querySelector("#output-progress");
const outputProgressLabel = document.querySelector("#output-progress-label");
const cropReview = document.querySelector("#crop-review");
const cropGrid = document.querySelector("#crop-grid");
const cropSort = document.querySelector("#crop-sort");
const selectionCount = document.querySelector("#selection-count");
const downloadCropsLink = document.querySelector("#download-crops");
const minSizeFilter = document.querySelector("#min-size-filter");
const maxSizeFilter = document.querySelector("#max-size-filter");
const minResolutionFilter = document.querySelector("#min-resolution-filter");
const minSizeValue = document.querySelector("#min-size-value");
const maxSizeValue = document.querySelector("#max-size-value");
const minResolutionValue = document.querySelector("#min-resolution-value");
const generateSelectedButton = document.querySelector("#generate-selected");
const historyMessage = document.querySelector("#history-message");
const previousRunsList = document.querySelector("#previous-runs-list");
const deleteRunsButton = document.querySelector("#delete-runs");
const settingsCookieName = "immich_face_sheet_settings";
const cookieMaxAge = 60 * 60 * 24 * 365;
let reviewJob = null;
let reviewStatusUrl = null;
let cropItems = [];
let selectedCropIds = new Set();
let outputGenerationPending = false;
let savedSettings = readSettingsCookie();

function setMessage(element, message, isError = false) {
  setProjectLinkedText(element, message);
  element.classList.toggle("error", isError);
}

function setProjectLinkedText(element, value) {
  const text = String(value ?? "");
  const parts = text.split(/\b(Immich)\b/gi);
  element.replaceChildren();
  for (const part of parts) {
    if (!/^Immich$/i.test(part)) {
      element.append(document.createTextNode(part));
      continue;
    }
    const link = document.createElement("a");
    link.className = "project-link";
    link.href = "https://immich.app/";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = part;
    element.append(link);
  }
}

function readSettingsCookie() {
  const prefix = `${settingsCookieName}=`;
  const cookie = document.cookie.split(";").map((value) => value.trim()).find((value) => value.startsWith(prefix));
  if (!cookie) return null;
  try {
    const value = JSON.parse(decodeURIComponent(cookie.slice(prefix.length)));
    return value && typeof value === "object" ? value : null;
  } catch (_error) {
    return null;
  }
}

function restoreLastExecutionSettings() {
  if (!savedSettings) return;
  const textFields = [
    "base_url", "person_id", "person_name", "output_name", "faces_per_row",
    "face_size", "photomosaic_tile_size", "face_spacing", "max_faces", "workers",
  ];
  for (const name of textFields) {
    const field = form.elements.namedItem(name);
    if (field && Object.hasOwn(savedSettings, name)) field.value = savedSettings[name];
  }
  if (["contact-sheet", "collage", "photomosaic"].includes(savedSettings.mode)) {
    modeSelect.value = savedSettings.mode;
  }
  for (const name of ["include_metadata", "save_crops", "force_fetch"]) {
    const checkbox = form.elements.namedItem(name);
    checkbox.checked = savedSettings[name] === true;
  }
}

function saveLastExecutionSettings() {
  // This allowlist deliberately omits api_key and the uploaded photomosaic file.
  const settings = {
    base_url: baseUrl.value,
    person_id: personSelect.value,
    person_name: personName.value,
    mode: modeSelect.value,
    output_name: form.elements.namedItem("output_name").value,
    faces_per_row: form.elements.namedItem("faces_per_row").value,
    face_size: form.elements.namedItem("face_size").value,
    photomosaic_tile_size: form.elements.namedItem("photomosaic_tile_size").value,
    face_spacing: form.elements.namedItem("face_spacing").value,
    max_faces: form.elements.namedItem("max_faces").value,
    workers: form.elements.namedItem("workers").value,
    include_metadata: form.elements.namedItem("include_metadata").checked,
    save_crops: form.elements.namedItem("save_crops").checked,
    force_fetch: form.elements.namedItem("force_fetch").checked,
  };
  try {
    const secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `${settingsCookieName}=${encodeURIComponent(JSON.stringify(settings))}; Max-Age=${cookieMaxAge}; Path=/; SameSite=Lax${secure}`;
    savedSettings = settings;
  } catch (_error) {
    // Cookie storage may be disabled by the browser; do not block the run.
  }
}

function formatFileSize(bytes) {
  let size = Number(bytes) || 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? size.toLocaleString() : size.toFixed(1)} ${units[unit]}`;
}

function updateFetchProgress(job) {
  if (job.status !== "running" && job.status !== "queued") {
    fetchProgressWrap.hidden = true;
    return;
  }

  fetchProgressWrap.hidden = false;
  if (job.status === "queued") {
    fetchProgress.removeAttribute("value");
    fetchProgressLabel.textContent = "Waiting to load face images…";
    return;
  }
  const current = Number(job.progress_current) || 0;
  const total = Number(job.progress_total) || 0;
  if (total > 0) {
    fetchProgress.max = total;
    fetchProgress.value = Math.min(current, total);
    const percent = Math.round(Math.min(current, total) / total * 100);
    const crops = Number(job.progress_crops) || 0;
    fetchProgressLabel.textContent = `${current.toLocaleString()} of ${total.toLocaleString()} assets · ${crops.toLocaleString()} face images (${percent}%)`;
  } else {
    fetchProgress.removeAttribute("value");
    fetchProgressLabel.textContent = "Finding matching assets…";
  }
}

function updateModeOptions() {
  const isMosaic = modeSelect.value === "photomosaic";
  const metadataCheckbox = document.querySelector("#metadata-option input");
  document.querySelector("#photomosaic-options").hidden = !isMosaic;
  document.querySelector("#layout-options").hidden = isMosaic;
  document.querySelector("#metadata-option").hidden = modeSelect.value !== "contact-sheet";
  metadataCheckbox.disabled = modeSelect.value !== "contact-sheet";
  document.querySelector("#photomosaic-image").required = isMosaic;
}

async function loadPeople() {
  setMessage(peopleMessage, "Loading people from Immich…");
  loadPeopleButton.disabled = true;
  createButton.disabled = true;
  personSelect.disabled = true;
  try {
    const response = await fetch("/api/people", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url: baseUrl.value, api_key: apiKey.value }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not load people.");

    personSelect.replaceChildren(new Option("Choose a person…", ""));
    for (const person of result.people) {
      const option = new Option(person.name, person.id);
      option.dataset.name = person.name;
      personSelect.add(option);
    }
    if (savedSettings?.base_url === baseUrl.value && savedSettings.person_id) {
      personSelect.value = savedSettings.person_id;
      personName.value = personSelect.selectedOptions[0]?.dataset.name || "";
    }
    personSelect.disabled = false;
    createButton.disabled = !personSelect.value;
    setMessage(peopleMessage, `Loaded ${result.people.length} named people.`);
  } catch (error) {
    personSelect.replaceChildren(new Option("Load people first", ""));
    setMessage(peopleMessage, error.message, true);
  } finally {
    loadPeopleButton.disabled = false;
  }
}

personSelect.addEventListener("change", () => {
  const option = personSelect.selectedOptions[0];
  personName.value = option?.dataset.name || "";
  createButton.disabled = !personSelect.value;
});

loadPeopleButton.addEventListener("click", loadPeople);
modeSelect.addEventListener("change", updateModeOptions);
createButton.addEventListener("click", async () => {
  if (!baseUrl.reportValidity()) return;
  if (!personSelect.value) {
    personSelect.reportValidity();
    return;
  }
  setMessage(jobMessage, "Starting image fetch…");
  setMessage(fetchStatus, "Submitting image selection…");
  setMessage(statusText, "");
  createButton.disabled = true;
  fetchProgressWrap.hidden = false;
  fetchProgress.removeAttribute("value");
  fetchProgressLabel.textContent = "Starting…";
  cropReview.hidden = true;
  outputSettingsFields.disabled = true;
  outputStepHint.hidden = false;
  outputProgressWrap.hidden = true;
  outputLogWrap.hidden = true;
  resultTitle.textContent = "Your output";
  imageWrap.hidden = true;
  downloadLink.hidden = true;
  resultPlaceholder.hidden = false;
  resultImage.removeAttribute("src");
  logView.textContent = "";
  fetchLogView.textContent = "";
  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      body: new FormData(form),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not load the face images.");
    setMessage(jobMessage, "Loading face images…");
    await watchJob(result.job_id, result.status_url);
  } catch (error) {
    fetchProgressWrap.hidden = true;
    setMessage(jobMessage, error.message, true);
    setMessage(fetchStatus, error.message, true);
  } finally {
    createButton.disabled = !personSelect.value;
  }
});
restoreLastExecutionSettings();
updateModeOptions();
loadPreviousRuns();

async function watchJob(jobId, statusUrl) {
  while (true) {
    await new Promise((resolve) => window.setTimeout(resolve, 1500));
    try {
      const response = await fetch(statusUrl, { cache: "no-store" });
      const job = await response.json();
      if (!response.ok) throw new Error(job.error || "Could not read job status.");
      const logText = job.logs.join("\n");
      setProjectLinkedText(fetchLogView, logText);
      setProjectLinkedText(logView, logText);
      if (outputProgressWrap.hidden) updateFetchProgress(job);
      else fetchProgressWrap.hidden = true;

      if (job.status === "review") {
        fetchProgressWrap.hidden = true;
        setMessage(fetchStatus, `Loaded ${Number(job.crop_count).toLocaleString()} face images. Review and select the ones to use.`);
        await loadCropReview(jobId, statusUrl);
        return;
      }
      if (job.status === "complete") {
        outputGenerationPending = false;
        updateSelectionCount();
        outputProgressWrap.hidden = true;
        resultPlaceholder.hidden = true;
        resultTitle.textContent = "Your output";
        setMessage(statusText, `Created ${job.output_name} for ${job.person_name}.`);
        resultImage.src = `${job.image_url}?v=${Date.now()}`;
        imageWrap.hidden = false;
        downloadLink.href = job.download_url;
        downloadLink.download = job.output_name;
        downloadLink.hidden = false;
        await loadPreviousRuns();
        return;
      }
      if (job.status === "failed") {
        fetchProgressWrap.hidden = true;
        if (outputProgressWrap.hidden) {
          setMessage(fetchStatus, job.error || "Could not load the face images. Review the job log below.", true);
        } else {
          outputGenerationPending = false;
          updateSelectionCount();
          outputProgressWrap.hidden = true;
          resultTitle.textContent = "Output could not be created";
          setMessage(statusText, job.error || "The job failed. Review the progress log.", true);
        }
        return;
      }
      if (job.status === "generating") {
        outputProgressWrap.hidden = false;
        outputProgress.removeAttribute("value");
        outputProgressLabel.textContent = "Creating your output…";
        setMessage(statusText, "Creating the image from your selected face images…");
      } else if (job.status === "queued" && !outputProgressWrap.hidden) {
        outputProgressWrap.hidden = false;
        outputProgress.removeAttribute("value");
        outputProgressLabel.textContent = "Waiting to create your output…";
        setMessage(statusText, "Waiting for a worker…");
      } else {
        setMessage(fetchStatus, job.status === "queued" ? "Waiting for a worker…" : "Fetching and cropping face images…");
      }
    } catch (error) {
      if (outputProgressWrap.hidden) setMessage(fetchStatus, error.message, true);
      else {
        outputGenerationPending = false;
        updateSelectionCount();
        outputProgressWrap.hidden = true;
        setMessage(statusText, error.message, true);
      }
      return;
    }
  }
}

async function loadPreviousRuns() {
  try {
    const response = await fetch("/api/runs", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not load previous runs.");

    previousRunsList.replaceChildren();
    for (const run of result.runs) {
      const item = document.createElement("li");
      item.className = "previous-run";
      const details = document.createElement("div");
      details.className = "previous-run-details";
      const title = document.createElement("strong");
      title.textContent = run.person_name || "Person";
      const metadata = document.createElement("span");
      const timestamp = Number(run.created_at);
      const modeLabels = {
        "contact-sheet": "Contact sheet",
        collage: "Collage",
        photomosaic: "Photomosaic",
      };
      const dateLabel = Number.isFinite(timestamp)
        ? new Date(timestamp * 1000).toLocaleString()
        : "Date unavailable";
      const fileCount = Number(run.file_count) || 0;
      const fileLabel = `${fileCount.toLocaleString()} ${fileCount === 1 ? "file" : "files"}`;
      metadata.textContent = `${modeLabels[run.mode] || run.mode || "Image"} · ${run.output_name} · ${dateLabel} · ${fileLabel} · ${formatFileSize(run.size_bytes)}`;
      details.append(title, metadata);

      const download = document.createElement("a");
      download.className = "button secondary";
      download.href = run.download_url;
      download.textContent = "Download";
      download.setAttribute("download", run.output_name || "face-sheet.jpg");
      const actions = document.createElement("div");
      actions.className = "previous-run-actions";
      if (run.crop_download_url) {
        const cropsDownload = document.createElement("a");
        cropsDownload.className = "button secondary";
        cropsDownload.href = run.crop_download_url;
        cropsDownload.textContent = "Download crops (.zip)";
        cropsDownload.setAttribute("download", "face-crops.zip");
        actions.append(cropsDownload);
      }
      const deleteButton = document.createElement("button");
      deleteButton.className = "button danger delete-run";
      deleteButton.type = "button";
      deleteButton.dataset.jobId = run.job_id;
      deleteButton.dataset.outputName = run.output_name || "";
      deleteButton.textContent = "Delete";
      deleteButton.setAttribute("aria-label", `Delete ${run.output_name || "result"} for ${run.person_name || "Person"}`);
      actions.prepend(download);
      actions.append(deleteButton);
      item.append(details, actions);
      previousRunsList.append(item);
    }
    deleteRunsButton.disabled = result.runs.length === 0;
    setMessage(historyMessage, result.runs.length
      ? `${result.runs.length.toLocaleString()} previous ${result.runs.length === 1 ? "result" : "results"} · ${Number(result.total_files || 0).toLocaleString()} files · ${formatFileSize(result.total_bytes)} total in run folders.`
      : "No previous runs yet.");
  } catch (error) {
    setMessage(historyMessage, error.message, true);
  }
}

previousRunsList.addEventListener("click", async (event) => {
  const button = event.target.closest(".delete-run");
  if (!button) return;
  const runId = button.dataset.jobId;
  const outputName = button.dataset.outputName;
  const runItem = button.closest(".previous-run");
  const runName = runItem.querySelector(".previous-run-details strong").textContent;
  if (!window.confirm(`Delete ${outputName || "this result"} for ${runName}? This cannot be undone.`)) return;

  button.disabled = true;
  setMessage(historyMessage, `Deleting the run for ${runName}…`);
  try {
    const response = await fetch(`/api/runs/${runId}/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ output_name: outputName }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not delete this run.");
    await loadPreviousRuns();
    setMessage(historyMessage, `Deleted ${outputName || "the result"} for ${runName}. ${historyMessage.textContent}`);
  } catch (error) {
    setMessage(historyMessage, error.message, true);
    button.disabled = false;
  }
});

deleteRunsButton.addEventListener("click", async () => {
  if (!window.confirm("Delete all previous run data, including generated images, crop previews, saved crops, and the reusable crop cache? This cannot be undone.")) return;
  deleteRunsButton.disabled = true;
  setMessage(historyMessage, "Deleting previous run data…");
  try {
    const response = await fetch("/api/runs/delete-all", { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not delete previous run data.");
    await loadPreviousRuns();
    const cacheMessage = result.shared_crops_kept
      ? " Shared crops and cache kept while a job is active."
      : " Saved crops and reusable cache cleared.";
    setMessage(historyMessage, `Deleted ${Number(result.deleted_runs || 0).toLocaleString()} previous ${result.deleted_runs === 1 ? "run" : "runs"}.${cacheMessage}`);
  } catch (error) {
    setMessage(historyMessage, error.message, true);
    deleteRunsButton.disabled = false;
  }
});

function updateSelectionCount() {
  const visibleCount = cropGrid.querySelectorAll(".crop-select").length;
  selectionCount.textContent = `${selectedCropIds.size.toLocaleString()} selected · ${visibleCount.toLocaleString()} shown · ${(cropItems.length - visibleCount).toLocaleString()} filtered out`;
  generateSelectedButton.disabled = selectedCropIds.size === 0 || outputGenerationPending;
}

function cropMatchesFilters(crop) {
  const sizeKb = Number(crop.file_size_bytes) / 1024;
  const resolution = Math.min(Number(crop.width) || 0, Number(crop.height) || 0);
  return sizeKb >= Number(minSizeFilter.value)
    && sizeKb <= Number(maxSizeFilter.value)
    && resolution >= Number(minResolutionFilter.value);
}

function updateCropFilterLabels() {
  minSizeValue.textContent = `${minSizeFilter.value} KB`;
  maxSizeValue.textContent = `${maxSizeFilter.value} KB`;
  minResolutionValue.textContent = minResolutionFilter.value === "0"
    ? "Any"
    : `${minResolutionFilter.value} px`;
}

function applyCropFilters() {
  const visibleIds = new Set(cropItems.filter(cropMatchesFilters).map((crop) => crop.id));
  selectedCropIds = new Set([...selectedCropIds].filter((cropId) => visibleIds.has(cropId)));
  updateCropFilterLabels();
  renderCropGrid();
}

function renderCropGrid() {
  const sortDirection = cropSort.value === "date-desc" ? -1 : 1;
  const sorted = cropItems.filter(cropMatchesFilters).sort((a, b) => {
    const dateA = Date.parse(a.date);
    const dateB = Date.parse(b.date);
    const validA = Number.isFinite(dateA);
    const validB = Number.isFinite(dateB);
    if (validA !== validB) return validA ? -1 : 1;
    if (validA && dateA !== dateB) return (dateA - dateB) * sortDirection;
    return a.id - b.id;
  });

  const fragment = document.createDocumentFragment();
  for (const crop of sorted) {
    const card = document.createElement("article");
    card.className = "crop-card";
    const image = document.createElement("img");
    image.src = crop.image_url;
    image.alt = `Face crop from ${crop.filename}`;
    image.loading = "lazy";
    image.decoding = "async";
    const info = document.createElement("div");
    info.className = "crop-info";
    const filename = document.createElement("span");
    filename.className = "crop-filename";
    filename.textContent = crop.filename;
    const date = document.createElement("span");
    date.className = "crop-date";
    date.textContent = crop.date ? crop.date.slice(0, 10) : "Date unavailable";
    const specs = document.createElement("span");
    specs.className = "crop-specs";
    specs.textContent = `${formatFileSize(crop.file_size_bytes)} · ${crop.width} × ${crop.height} px`;
    const label = document.createElement("label");
    label.className = "crop-choice";
    const checkbox = document.createElement("input");
    checkbox.className = "crop-select";
    checkbox.type = "checkbox";
    checkbox.value = crop.id;
    checkbox.checked = selectedCropIds.has(crop.id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selectedCropIds.add(crop.id);
      else selectedCropIds.delete(crop.id);
      updateSelectionCount();
    });
    const choiceText = document.createElement("span");
    choiceText.textContent = "Use this crop";
    label.append(checkbox, choiceText);
    info.append(filename, date, specs, label);
    card.append(image, info);
    fragment.append(card);
  }
  cropGrid.replaceChildren(fragment);
  updateSelectionCount();
}

async function loadCropReview(jobId, statusUrl) {
  reviewJob = jobId;
  reviewStatusUrl = statusUrl;
  cropReview.hidden = false;
  generateSelectedButton.disabled = true;
  try {
    const response = await fetch(`/api/jobs/${jobId}/crops`, { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not load the cropped images.");
    cropItems = result.crops;
    selectedCropIds = new Set(cropItems.map((crop) => crop.id));
    const largestCropKb = Math.ceil(cropItems.reduce((largest, crop) => Math.max(largest, Number(crop.file_size_bytes) || 0), 0) / 1024);
    const maxSizeKb = Math.max(1024, largestCropKb);
    minSizeFilter.max = String(maxSizeKb);
    maxSizeFilter.max = String(maxSizeKb);
    minSizeFilter.value = "0";
    maxSizeFilter.value = String(maxSizeKb);
    const largestShortSide = cropItems.reduce((largest, crop) => Math.max(largest, Math.min(Number(crop.width) || 0, Number(crop.height) || 0)), 0);
    minResolutionFilter.max = String(Math.max(1, largestShortSide));
    minResolutionFilter.step = String(Math.max(1, Math.floor(largestShortSide / 128)));
    minResolutionFilter.value = "0";
    downloadCropsLink.href = `/api/jobs/${jobId}/crops.zip`;
    updateCropFilterLabels();
    renderCropGrid();
    outputSettingsFields.disabled = false;
    outputStepHint.hidden = true;
    cropReview.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setMessage(fetchStatus, error.message, true);
    generateSelectedButton.disabled = true;
  }
}

document.querySelector("#select-all").addEventListener("click", () => {
  selectedCropIds = new Set(cropItems.filter(cropMatchesFilters).map((crop) => crop.id));
  cropGrid.querySelectorAll(".crop-select").forEach((checkbox) => { checkbox.checked = true; });
  updateSelectionCount();
});

document.querySelector("#deselect-all").addEventListener("click", () => {
  selectedCropIds.clear();
  cropGrid.querySelectorAll(".crop-select").forEach((checkbox) => { checkbox.checked = false; });
  updateSelectionCount();
});

cropSort.addEventListener("change", renderCropGrid);
minSizeFilter.addEventListener("input", () => {
  if (Number(minSizeFilter.value) > Number(maxSizeFilter.value)) {
    maxSizeFilter.value = minSizeFilter.value;
  }
  applyCropFilters();
});
maxSizeFilter.addEventListener("input", () => {
  if (Number(maxSizeFilter.value) < Number(minSizeFilter.value)) {
    minSizeFilter.value = maxSizeFilter.value;
  }
  applyCropFilters();
});
minResolutionFilter.addEventListener("input", applyCropFilters);

generateSelectedButton.addEventListener("click", async () => {
  if (!reviewJob) return;
  if (!form.reportValidity()) return;
  const selectedIds = [...cropGrid.querySelectorAll(".crop-select:checked")].map((checkbox) => Number(checkbox.value));
  saveLastExecutionSettings();
  outputGenerationPending = true;
  generateSelectedButton.disabled = true;
  resultPanel.hidden = false;
  outputProgressWrap.hidden = false;
  outputLogWrap.hidden = false;
  resultPlaceholder.hidden = true;
  imageWrap.hidden = true;
  downloadLink.hidden = true;
  resultImage.removeAttribute("src");
  outputProgress.removeAttribute("value");
  outputProgressLabel.textContent = "Starting output…";
  resultTitle.textContent = "Your output";
  setMessage(statusText, "Creating the image from your selected face images…");
  logView.textContent = "";
  try {
    const response = await fetch(`/api/jobs/${reviewJob}/generate`, {
      method: "POST",
      body: (() => {
        const data = new FormData(form);
        data.append("selected_ids", JSON.stringify(selectedIds));
        return data;
      })(),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not create the image.");
    await watchJob(reviewJob, reviewStatusUrl);
  } catch (error) {
    outputGenerationPending = false;
    updateSelectionCount();
    outputProgressWrap.hidden = true;
    setMessage(statusText, error.message, true);
    generateSelectedButton.disabled = false;
  }
});
