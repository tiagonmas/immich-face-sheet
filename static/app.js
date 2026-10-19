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

function setMessage(element, message, isError = false) {
  element.textContent = message;
  element.classList.toggle("error", isError);
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
    personSelect.disabled = false;
    setMessage(peopleMessage, `Loaded ${result.people.length} people.`);
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
updateModeOptions();

async function watchJob(jobId, statusUrl) {
  while (true) {
    await new Promise((resolve) => window.setTimeout(resolve, 1500));
    try {
      const response = await fetch(statusUrl, { cache: "no-store" });
      const job = await response.json();
      if (!response.ok) throw new Error(job.error || "Could not read job status.");
      logView.textContent = job.logs.join("\n");

      if (job.status === "complete") {
        resultTitle.textContent = "Image ready";
        setMessage(statusText, `Created ${job.output_name} for ${job.person_name}.`);
        resultImage.src = `${job.image_url}?v=${Date.now()}`;
        imageWrap.hidden = false;
        downloadLink.href = job.download_url;
        downloadLink.download = job.output_name;
        downloadLink.hidden = false;
        return;
      }
      if (job.status === "failed") {
        resultTitle.textContent = "Could not create the image";
        setMessage(statusText, job.error || "The job failed. Review the progress log.", true);
        return;
      }
      setMessage(statusText, job.status === "queued" ? "Waiting for a worker…" : "Creating the image…");
    } catch (error) {
      setMessage(statusText, error.message, true);
      return;
    }
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setMessage(jobMessage, "Starting job…");
  createButton.disabled = true;
  resultPanel.hidden = false;
  imageWrap.hidden = true;
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
