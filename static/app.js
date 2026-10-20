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
const resultPanel = document.querySelector("#result-panel");
const resultTitle = document.querySelector("#result-title");
const statusText = document.querySelector("#job-status");
const logView = document.querySelector("#job-log");
const imageWrap = document.querySelector("#image-wrap");
const resultImage = document.querySelector("#result-image");
const downloadLink = document.querySelector("#download-link");
const jobProgressWrap = document.querySelector("#job-progress-wrap");
const jobProgress = document.querySelector("#job-progress");
const jobProgressLabel = document.querySelector("#job-progress-label");
const cropReview = document.querySelector("#crop-review");
const cropGrid = document.querySelector("#crop-grid");
const cropSort = document.querySelector("#crop-sort");
const selectionCount = document.querySelector("#selection-count");
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
let savedSettings = readSettingsCookie();

function setMessage(element, message, isError = false) {
  element.textContent = message;
  element.classList.toggle("error", isError);
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

function updateJobProgress(job) {
  if (job.status !== "running") {
    jobProgressWrap.hidden = true;
    return;
  }

  jobProgressWrap.hidden = false;
  const current = Number(job.progress_current) || 0;
  const total = Number(job.progress_total) || 0;
  if (total > 0) {
    jobProgress.max = total;
    jobProgress.value = Math.min(current, total);
    const percent = Math.round(Math.min(current, total) / total * 100);
    const crops = Number(job.progress_crops) || 0;
    jobProgressLabel.textContent = `${current.toLocaleString()} of ${total.toLocaleString()} assets · ${crops.toLocaleString()} crops (${percent}%)`;
  } else {
    jobProgress.removeAttribute("value");
    jobProgressLabel.textContent = "Finding matching assets…";
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
      const label = `${person.name} — ${person.asset_count} assets`;
      const option = new Option(label, person.id);
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
      logView.textContent = job.logs.join("\n");
      updateJobProgress(job);

      if (job.status === "review") {
        resultTitle.textContent = "Review face crops";
        setMessage(statusText, `Choose which of the ${job.crop_count} cropped images to use.`);
        await loadCropReview(jobId, statusUrl);
        return;
      }
      if (job.status === "complete") {
        cropReview.hidden = true;
        resultTitle.textContent = "Image ready";
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
        cropReview.hidden = true;
        resultTitle.textContent = "Could not create the image";
        setMessage(statusText, job.error || "The job failed. Review the progress log.", true);
        return;
      }
      setMessage(statusText, job.status === "queued" ? "Waiting for a worker…" : job.status === "generating" ? "Creating the image…" : "Fetching and cropping images…");
    } catch (error) {
      setMessage(statusText, error.message, true);
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
      const deleteButton = document.createElement("button");
      deleteButton.className = "button danger delete-run";
      deleteButton.type = "button";
      deleteButton.dataset.jobId = run.job_id;
      deleteButton.textContent = "Delete";
      deleteButton.setAttribute("aria-label", `Delete run for ${run.person_name || "Person"}`);
      actions.append(download, deleteButton);
      item.append(details, actions);
      previousRunsList.append(item);
    }
    deleteRunsButton.disabled = result.runs.length === 0;
    setMessage(historyMessage, result.runs.length
      ? `${result.runs.length.toLocaleString()} completed ${result.runs.length === 1 ? "run" : "runs"} · ${Number(result.total_files || 0).toLocaleString()} files · ${formatFileSize(result.total_bytes)} total in run folders.`
      : "No previous runs yet.");
  } catch (error) {
    setMessage(historyMessage, error.message, true);
  }
}

previousRunsList.addEventListener("click", async (event) => {
  const button = event.target.closest(".delete-run");
  if (!button) return;
  const runId = button.dataset.jobId;
  const runItem = button.closest(".previous-run");
  const runName = runItem.querySelector(".previous-run-details strong").textContent;
  if (!window.confirm(`Delete the previous run for ${runName}, including all files stored for that run? This cannot be undone.`)) return;

  button.disabled = true;
  setMessage(historyMessage, `Deleting the run for ${runName}…`);
  try {
    const response = await fetch(`/api/runs/${runId}/delete`, { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not delete this run.");
    await loadPreviousRuns();
    setMessage(historyMessage, `Deleted the run for ${runName}. ${historyMessage.textContent}`);
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
  const selected = cropGrid.querySelectorAll(".crop-select:checked").length;
  selectionCount.textContent = `${selected.toLocaleString()} of ${cropItems.length.toLocaleString()} selected`;
  generateSelectedButton.disabled = selected === 0;
}

function renderCropGrid() {
  const sortDirection = cropSort.value === "date-desc" ? -1 : 1;
  const sorted = [...cropItems].sort((a, b) => {
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
    info.append(filename, date, label);
    card.append(image, info);
    fragment.append(card);
  }
  cropGrid.replaceChildren(fragment);
  updateSelectionCount();
}

async function loadCropReview(jobId, statusUrl) {
  reviewJob = jobId;
  reviewStatusUrl = statusUrl;
  resultImage.removeAttribute("src");
  imageWrap.hidden = true;
  downloadLink.hidden = true;
  cropReview.hidden = false;
  generateSelectedButton.disabled = true;
  try {
    const response = await fetch(`/api/jobs/${jobId}/crops`, { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not load the cropped images.");
    cropItems = result.crops;
    selectedCropIds = new Set(cropItems.map((crop) => crop.id));
    renderCropGrid();
    cropReview.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setMessage(statusText, error.message, true);
    generateSelectedButton.disabled = true;
  }
}

document.querySelector("#select-all").addEventListener("click", () => {
  selectedCropIds = new Set(cropItems.map((crop) => crop.id));
  cropGrid.querySelectorAll(".crop-select").forEach((checkbox) => { checkbox.checked = true; });
  updateSelectionCount();
});

document.querySelector("#deselect-all").addEventListener("click", () => {
  selectedCropIds.clear();
  cropGrid.querySelectorAll(".crop-select").forEach((checkbox) => { checkbox.checked = false; });
  updateSelectionCount();
});

cropSort.addEventListener("change", renderCropGrid);

generateSelectedButton.addEventListener("click", async () => {
  if (!reviewJob) return;
  const selectedIds = [...cropGrid.querySelectorAll(".crop-select:checked")].map((checkbox) => Number(checkbox.value));
  generateSelectedButton.disabled = true;
  setMessage(statusText, "Creating the image from your selected crops…");
  try {
    const response = await fetch(`/api/jobs/${reviewJob}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selected_ids: selectedIds }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not create the image.");
    cropReview.hidden = true;
    resultTitle.textContent = "Creating image";
    await watchJob(reviewJob, reviewStatusUrl);
  } catch (error) {
    setMessage(statusText, error.message, true);
    generateSelectedButton.disabled = false;
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  saveLastExecutionSettings();
  setMessage(jobMessage, "Starting job…");
  createButton.disabled = true;
  resultPanel.hidden = false;
  jobProgressWrap.hidden = true;
  imageWrap.hidden = true;
  cropReview.hidden = true;
  downloadLink.hidden = true;
  logView.textContent = "";
  resultTitle.textContent = "Processing";
  setMessage(statusText, "Submitting options…");

  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      body: new FormData(form),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not start the job.");
    setMessage(jobMessage, "Job started.");
    await watchJob(result.job_id, result.status_url);
  } catch (error) {
    setMessage(jobMessage, error.message, true);
    setMessage(statusText, error.message, true);
  } finally {
    createButton.disabled = !personSelect.value;
  }
});
