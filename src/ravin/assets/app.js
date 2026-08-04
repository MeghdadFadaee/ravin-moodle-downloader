const state = { catalog: null, course: null, query: "", filter: "all" };

const byId = (id) => document.getElementById(id);
const clampPercent = (value) => Math.max(0, Math.min(100, Math.round(value || 0)));

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  const digits = value >= 10 || index === 0 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[index]}`;
}

function normalized(value) {
  return String(value || "").toLocaleLowerCase().trim();
}

function setText(id, value) {
  const element = byId(id);
  if (element) element.textContent = value;
}

function appendMarkdownInline(parent, text) {
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    const token = match[0];
    if (token.startsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = token.slice(2, -2);
      parent.append(strong);
    } else if (token.startsWith("`")) {
      const code = document.createElement("code");
      code.textContent = token.slice(1, -1);
      parent.append(code);
    } else {
      const parts = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      const link = document.createElement("a");
      link.textContent = parts[1];
      if (/^(https?:\/\/|\.\.?\/)/.test(parts[2])) {
        link.href = parts[2];
        link.target = "_blank";
        link.rel = "noopener";
      }
      parent.append(link);
    }
    offset = match.index + token.length;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}

function markdownCells(line) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
}

function renderMarkdown(target, source) {
  target.innerHTML = "";
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  let list = null;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.trim()) {
      list = null;
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      list = null;
      const element = document.createElement(`h${heading[1].length}`);
      appendMarkdownInline(element, heading[2]);
      target.append(element);
      continue;
    }
    if (line.includes("|") && index + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1])) {
      list = null;
      const table = document.createElement("table");
      const head = document.createElement("thead");
      const headRow = document.createElement("tr");
      markdownCells(line).forEach((cell) => {
        const th = document.createElement("th");
        appendMarkdownInline(th, cell);
        headRow.append(th);
      });
      head.append(headRow);
      table.append(head);
      const body = document.createElement("tbody");
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        const row = document.createElement("tr");
        markdownCells(lines[index]).forEach((cell) => {
          const td = document.createElement("td");
          appendMarkdownInline(td, cell);
          row.append(td);
        });
        body.append(row);
        index += 1;
      }
      index -= 1;
      table.append(body);
      target.append(table);
      continue;
    }
    const bullet = line.match(/^\s*[-*]\s+(.+)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (bullet || numbered) {
      const type = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== type) {
        list = document.createElement(type);
        target.append(list);
      }
      const item = document.createElement("li");
      appendMarkdownInline(item, (bullet || numbered)[1]);
      list.append(item);
      continue;
    }
    list = null;
    const quote = line.match(/^>\s?(.*)$/);
    const paragraph = document.createElement(quote ? "blockquote" : "p");
    appendMarkdownInline(paragraph, quote ? quote[1] : line);
    target.append(paragraph);
  }
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("ravin-library-theme", theme);
}

function setupTheme() {
  const stored = localStorage.getItem("ravin-library-theme");
  const preferred = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  applyTheme(stored || preferred);
  byId("themeToggle")?.addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
}

async function loadCatalog() {
  const response = await fetch("courses.json", { cache: "no-store" });
  if (!response.ok) throw new Error(`Could not load courses.json (${response.status})`);
  return response.json();
}

function showLoadError(error) {
  const target = byId("courseGrid") || byId("sectionList");
  if (!target) return;
  target.innerHTML = "";
  const box = document.createElement("div");
  box.className = "empty-state";
  const title = document.createElement("strong");
  title.textContent = "The course data could not be loaded";
  const detail = document.createElement("span");
  detail.textContent = "Open this library with ‘ravin serve-library’, then refresh the page.";
  box.append(title, detail);
  target.append(box);
  console.error(error);
}

function courseCard(course) {
  const percent = clampPercent((course.downloaded_count / Math.max(course.file_count, 1)) * 100);
  const card = document.createElement("article");
  card.className = "course-card";

  const top = document.createElement("div");
  top.className = "course-card-top";
  const code = document.createElement("span");
  code.className = "course-code";
  code.textContent = course.shortname || `Course ${course.id}`;
  const badge = document.createElement("span");
  badge.className = "course-badge";
  badge.textContent = `${course.activity_count ?? course.file_count} activities`;
  top.append(code, badge);

  const title = document.createElement("h3");
  title.dir = "auto";
  title.textContent = course.fullname;
  const meta = document.createElement("p");
  meta.className = "course-card-meta";
  meta.textContent = `${course.section_count} chapters · ${course.file_count} files · ${formatBytes(course.downloaded_bytes)} offline`;

  const bottom = document.createElement("div");
  bottom.className = "course-card-bottom";
  const track = document.createElement("div");
  track.className = "progress-track";
  const fill = document.createElement("span");
  fill.style.setProperty("--progress", `${percent}%`);
  track.append(fill);
  const progress = document.createElement("div");
  progress.className = "progress-copy";
  progress.innerHTML = `<span>${course.downloaded_count} offline</span><strong>${percent}%</strong>`;
  const link = document.createElement("a");
  link.className = "course-link";
  link.href = `course.html?id=${encodeURIComponent(course.id)}`;
  link.innerHTML = `Open course <span aria-hidden="true">→</span>`;
  bottom.append(track, progress, link);
  card.append(top, title, meta, bottom);
  return card;
}

function renderCourseGrid() {
  const grid = byId("courseGrid");
  const query = normalized(byId("courseSearch")?.value);
  const courses = state.catalog.courses.filter((course) => normalized(`${course.fullname} ${course.shortname}`).includes(query));
  grid.innerHTML = "";
  courses.forEach((course) => grid.append(courseCard(course)));
  byId("emptyCourses").hidden = courses.length !== 0;
}

function renderIndex() {
  const stats = state.catalog.stats;
  const percent = clampPercent((stats.downloaded_files / Math.max(stats.files, 1)) * 100);
  setText("heroPercent", `${percent}%`);
  setText("courseCount", stats.courses.toLocaleString());
  setText("resourceCount", (stats.activities ?? stats.files).toLocaleString());
  setText("downloadedCount", stats.downloaded_files.toLocaleString());
  setText("librarySize", formatBytes(stats.downloaded_bytes));
  setText("generatedAt", `Updated ${new Date(state.catalog.generated_at).toLocaleString()}`);
  byId("courseSearch").addEventListener("input", renderCourseGrid);
  renderCourseGrid();
}

function completionKey(item) {
  return `ravin-complete-${state.course.id}-${item.id}`;
}

function isComplete(item) {
  return localStorage.getItem(completionKey(item)) === "1";
}

function resourceMatches(item) {
  const haystack = normalized(`${item.title} ${item.filename} ${item.section}`);
  if (state.query && !haystack.includes(state.query)) return false;
  if (state.filter === "all") return true;
  if (state.filter === "downloaded" || state.filter === "missing") return item.status === state.filter;
  return item.kind === state.filter;
}

function resourceRow(item, index) {
  const row = document.createElement("article");
  row.className = `resource-row${isComplete(item) ? " completed" : ""}`;
  const number = document.createElement("span");
  number.className = "resource-index";
  number.textContent = String(index + 1).padStart(2, "0");

  const main = document.createElement("div");
  main.className = "resource-main";
  const title = document.createElement("span");
  title.className = "resource-title";
  title.dir = "auto";
  title.textContent = item.title;
  const filename = document.createElement("span");
  filename.className = "resource-file";
  filename.dir = "auto";
  filename.textContent = item.filename || item.badge || item.activity_type;
  main.append(title, filename);
  if (item.description) {
    const description = document.createElement("span");
    description.className = "resource-description";
    description.dir = "auto";
    description.textContent = item.description;
    main.append(description);
  }
  const artifactLabels = {
    questions: "Questions",
    summary: "Summary",
    transcript: "Transcript",
  };
  const availableArtifacts = Object.keys(artifactLabels).filter((kind) => item.artifacts?.[kind]?.url);
  if (availableArtifacts.length) {
    const artifactActions = document.createElement("div");
    artifactActions.className = "artifact-actions";
    availableArtifacts.forEach((kind) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "artifact-button";
      button.textContent = artifactLabels[kind];
      button.addEventListener("click", () => openArtifact(item, kind));
      artifactActions.append(button);
    });
    main.append(artifactActions);
  }

  const meta = document.createElement("div");
  meta.className = "resource-meta";
  const type = document.createElement("span");
  type.className = "type-pill";
  type.textContent = item.kind;
  const dot = document.createElement("span");
  dot.className = `status-dot${item.status === "missing" ? " missing" : item.status === "online" ? " online" : ""}`;
  const size = document.createElement("span");
  size.textContent = item.status === "partial"
    ? `${formatBytes(item.local_bytes)} partial`
    : item.status === "online"
      ? (item.lms_completed ? "LMS complete" : "Online")
      : formatBytes(item.size);
  meta.append(type, dot, size);

  let action;
  if (item.status === "downloaded" && item.kind === "video") {
    action = document.createElement("button");
    action.type = "button";
    action.textContent = "Play";
    action.addEventListener("click", () => openVideo(item));
  } else if (item.status === "downloaded") {
    action = document.createElement("a");
    action.href = item.local_url;
    action.target = "_blank";
    action.rel = "noopener";
    action.textContent = item.kind === "document" ? "Open" : "View";
  } else if (item.status === "online" && item.source_url) {
    action = document.createElement("a");
    action.href = item.source_url;
    action.target = "_blank";
    action.rel = "noopener";
    action.textContent = "Open LMS";
  } else {
    action = document.createElement("span");
    action.textContent = item.status === "partial" ? "Partial" : "Missing";
    action.setAttribute("aria-disabled", "true");
  }
  action.className = "action-button";

  const complete = document.createElement("input");
  complete.className = "complete-check";
  complete.type = "checkbox";
  complete.checked = isComplete(item);
  complete.setAttribute("aria-label", `Mark ${item.title} complete`);
  complete.addEventListener("change", () => {
    localStorage.setItem(completionKey(item), complete.checked ? "1" : "0");
    row.classList.toggle("completed", complete.checked);
  });
  row.append(number, main, meta, action, complete);
  return row;
}

function renderResources() {
  const container = byId("sectionList");
  container.innerHTML = "";
  let visibleCount = 0;
  let runningIndex = 0;
  state.course.sections.forEach((section) => {
    const items = section.items.filter(resourceMatches);
    if (!items.length) return;
    visibleCount += items.length;
    const wrapper = document.createElement("section");
    wrapper.className = "resource-section";
    const heading = document.createElement("div");
    heading.className = "resource-section-header";
    const title = document.createElement("h2");
    title.dir = "auto";
    title.textContent = section.name;
    const count = document.createElement("span");
    count.textContent = `${items.length} resource${items.length === 1 ? "" : "s"}`;
    heading.append(title, count);
    if (section.summary) {
      const summary = document.createElement("p");
      summary.className = "section-summary";
      summary.dir = "auto";
      summary.textContent = section.summary;
      heading.append(summary);
    }
    const list = document.createElement("div");
    list.className = "resource-list";
    items.forEach((item) => {
      list.append(resourceRow(item, runningIndex));
      runningIndex += 1;
    });
    wrapper.append(heading, list);
    container.append(wrapper);
  });
  byId("emptyResources").hidden = visibleCount !== 0;
}

function openVideo(item) {
  const dialog = byId("mediaDialog");
  const player = byId("mediaPlayer");
  setText("mediaTitle", item.title);
  player.src = item.local_url;
  dialog.showModal();
  player.play().catch(() => {});
}

function closeVideo() {
  const dialog = byId("mediaDialog");
  const player = byId("mediaPlayer");
  player.pause();
  player.removeAttribute("src");
  player.load();
  dialog.close();
}

async function openArtifact(item, kind) {
  const artifact = item.artifacts?.[kind];
  if (!artifact) return;
  const dialog = byId("artifactDialog");
  const content = byId("artifactContent");
  const labels = { questions: "Exam questions", summary: "Lesson summary", transcript: "Transcript" };
  setText("artifactEyebrow", labels[kind] || "Course material");
  setText("artifactTitle", item.title);
  content.innerHTML = '<p class="artifact-loading">Loading…</p>';
  dialog.showModal();
  try {
    const response = await fetch(artifact.url, { cache: "no-store" });
    if (!response.ok) throw new Error(`Could not load ${artifact.url} (${response.status})`);
    const source = await response.text();
    if (artifact.format === "markdown") renderMarkdown(content, source);
    else {
      content.innerHTML = "";
      const transcript = document.createElement("pre");
      transcript.className = "transcript-text";
      transcript.textContent = source;
      content.append(transcript);
    }
  } catch (error) {
    content.textContent = "This material could not be loaded.";
    console.error(error);
  }
}

function closeArtifact() {
  byId("artifactDialog").close();
  byId("artifactContent").innerHTML = "";
}

function renderCourse() {
  const id = new URLSearchParams(window.location.search).get("id");
  state.course = state.catalog.courses.find((course) => String(course.id) === String(id));
  if (!state.course) {
    showLoadError(new Error(`Course ${id || "(missing)"} was not found`));
    return;
  }
  const course = state.course;
  document.title = `${course.fullname} · Learning Library`;
  const percent = clampPercent((course.downloaded_count / Math.max(course.file_count, 1)) * 100);
  setText("breadcrumbCourse", course.fullname);
  setText("courseCode", course.shortname || `Course ${course.id}`);
  setText("courseTitle", course.fullname);
  setText("courseSummary", `${course.activity_count ?? course.file_count} activities, ${course.file_count} downloadable files, and ${course.section_count} chapters.`);
  setText("coursePercent", `${percent}%`);
  setText("courseDownloaded", `${course.downloaded_count} of ${course.file_count}`);
  setText("generatedAt", `Updated ${new Date(state.catalog.generated_at).toLocaleString()}`);
  byId("courseProgress").style.setProperty("--progress", percent);
  byId("courseSource").href = course.source_url;
  byId("resourceSearch").addEventListener("input", (event) => {
    state.query = normalized(event.target.value);
    renderResources();
  });
  byId("resourceFilters").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-filter]");
    if (!button) return;
    state.filter = button.dataset.filter;
    document.querySelectorAll("button[data-filter]").forEach((item) => item.classList.toggle("active", item === button));
    renderResources();
  });
  byId("closeMedia").addEventListener("click", closeVideo);
  byId("mediaDialog").addEventListener("click", (event) => {
    if (event.target === byId("mediaDialog")) closeVideo();
  });
  byId("closeArtifact").addEventListener("click", closeArtifact);
  byId("artifactDialog").addEventListener("click", (event) => {
    if (event.target === byId("artifactDialog")) closeArtifact();
  });
  renderResources();
}

async function start() {
  setupTheme();
  try {
    state.catalog = await loadCatalog();
    if (document.body.dataset.page === "course") renderCourse();
    else renderIndex();
  } catch (error) {
    showLoadError(error);
  }
}

start();
