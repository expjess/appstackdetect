const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const form = $("#lookup");
const query = $("#query");
const goBtn = $("#go");
const progress = $("#progress");
const steps = $("#steps");
const errorBox = $("#error");
const resultBox = $("#result");
const drop = $("#drop");
const fileInput = $("#file");

let polling = null;

form.addEventListener("submit", (e) => {
  e.preventDefault();
  startUrl(query.value);
});

$("#pick").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => fileInput.files[0] && startUpload(fileInput.files[0]));

["dragenter", "dragover"].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.add("over");
  })
);
["dragleave", "drop"].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.remove("over");
  })
);
drop.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) startUpload(f);
});

function reset(label) {
  clearInterval(polling);
  errorBox.hidden = true;
  resultBox.hidden = true;
  resultBox.innerHTML = "";
  steps.innerHTML = "";
  progress.hidden = false;
  goBtn.disabled = true;
  addStep(label);
}

function addStep(text) {
  const li = el("li", null, text);
  steps.appendChild(li);
}

async function startUrl(value) {
  if (!value.trim()) return;
  reset("Resolving " + value.trim());
  try {
    const resp = await fetch("/api/jobs/url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input: value }),
    });
    const data = await resp.json();
    if (!resp.ok) return fail(data.error || "Request failed.");
    watch(data.job);
  } catch (err) {
    fail(String(err));
  }
}

async function startUpload(file) {
  reset(`Uploading ${file.name} (${(file.size / 1e6).toFixed(1)} MB)`);
  const body = new FormData();
  body.append("file", file);
  body.append("name", file.name);
  try {
    const resp = await fetch("/api/jobs/upload", { method: "POST", body });
    const data = await resp.json();
    if (!resp.ok) return fail(data.error || "Upload failed.");
    watch(data.job);
  } catch (err) {
    fail(String(err));
  }
}

let CURRENT_JOB = null;

function watch(jobId) {
  CURRENT_JOB = jobId;
  let seen = 0;
  polling = setInterval(async () => {
    const resp = await fetch(`/api/jobs/${jobId}`);
    if (!resp.ok) return fail("The job expired.");
    const job = await resp.json();
    job.steps.slice(seen).forEach((s) => addStep(s.text));
    seen = job.steps.length;
    if (job.status === "error") return fail(job.error);
    if (job.status === "done") {
      clearInterval(polling);
      goBtn.disabled = false;
      progress.hidden = true;
      render(job.result);
    }
  }, 700);
}

function fail(message) {
  clearInterval(polling);
  goBtn.disabled = false;
  progress.hidden = true;
  errorBox.hidden = false;
  errorBox.textContent = message;
}

// ---------------------------------------------------------------- rendering

function render(r) {
  resultBox.hidden = false;
  resultBox.innerHTML = "";

  const store = r.store_ios || r.store || {};
  const card = el("div", "card");

  // app header
  const bar = el("div", "appbar");
  if (store.icon) {
    const img = el("img");
    img.src = store.icon;
    img.alt = "";
    bar.appendChild(img);
  }
  const meta = el("div", "meta");
  const app = r.app || {};
  meta.appendChild(el("div", "name", store.name || app.name || (r.file && r.file.name) || "Unknown app"));
  const ident = app.package || app.bundle_id || store.package || store.bundle_id || "";
  const version = app.version_name || store.version || "";
  meta.appendChild(el("div", "idline", [ident, version && "v" + version, store.developer].filter(Boolean).join("  ·  ")));
  bar.appendChild(meta);
  card.appendChild(bar);

  if (r.ios_only) {
    renderIosOnly(r, card, store);
    return;
  }

  const v = r.verdict || {};
  const verdicts = el("div", "verdicts");
  verdicts.appendChild(
    verdictTile("React Native", v.react_native ? "Yes" : "No", v.react_native, r.stack && r.stack.react_native_version ? "version " + r.stack.react_native_version : v.framework)
  );
  const expoNote =
    v.expo_level === "expo-app"
      ? "Expo app config is embedded in the build"
      : v.expo_level === "expo-modules"
      ? "Expo packages, no embedded app config"
      : v.expo_level === "expo-go"
      ? "this archive is the Expo Go client"
      : "no Expo packages found";
  const expoValue =
    v.expo_level === "expo-go"
      ? "Expo Go"
      : v.uses_expo
      ? r.expo && r.expo.sdk_version
        ? "SDK " + r.expo.sdk_version
        : "Yes"
      : "No";
  const expoTile = verdictTile("Expo", expoValue, v.uses_expo, expoNote);
  expoTile.classList.add("expo");
  verdicts.appendChild(expoTile);
  verdicts.appendChild(verdictTile("Framework", v.framework || "—", null, "confidence: " + (v.framework_confidence || "—")));
  card.appendChild(verdicts);
  card.appendChild(el("p", "summary", v.summary || ""));

  if (r.ios_inference) {
    const inf = r.ios_inference;
    const note = el("div", "notes");
    note.appendChild(
      el(
        "p",
        null,
        `You asked about an iOS app. Apple ships no downloadable binary, so the Android build under the same identifier (${inf.analyzed.package}) was analyzed instead.`
      )
    );
    note.appendChild(el("p", null, inf.explanation));
    card.appendChild(note);
  }

  resultBox.appendChild(card);

  // stack facts
  const facts = [];
  const s = r.stack || {};
  const push = (k, val) => val !== undefined && val !== "" && val !== null && facts.push([k, String(val)]);
  push("Platform analyzed", app.platform);
  push("React Native", s.react_native_version);
  push("React", s.react_version);
  push("Expo SDK", r.expo && r.expo.sdk_version);
  push("JS engine", s.js_engine);
  push("New architecture", s.new_architecture);
  if (s.js_bundle) {
    push("JS bundle", `${s.js_bundle.format}, ${(s.js_bundle.size / 1e6).toFixed(1)} MB`);
  }
  push("Version", app.version_name);
  push("Version code", app.version_code);
  push("ABIs", (app.abis || []).join(", "));
  push("Min SDK / OS", app.min_sdk || app.min_os);
  push("dex files", app.dex_files);
  push("Archive size", r.file && (r.file.size / 1e6).toFixed(1) + " MB");
  if (facts.length) {
    resultBox.appendChild(section("Build facts", gridOf(facts)));
  }

  // expo detail
  const e = r.expo || {};
  if (e.uses_expo) {
    const ex = [];
    const pushE = (k, val) => val && ex.push([k, String(val)]);
    pushE("Expo SDK", e.sdk_version);
    pushE("Project slug", e.slug);
    pushE("Owner", e.owner);
    pushE("EAS project id", e.eas_project_id);
    pushE("Updates service", e.update_service);
    pushE("Updates URL", e.update_url);
    pushE("Updates enabled", e.updates_enabled);
    pushE("Check on launch", e.updates_check_on_launch);
    pushE("Update code signing", e.update_code_signing);
    pushE("Runtime version", e.runtime_version);
    pushE("Runtime version policy", e.runtime_version_policy);
    pushE("Embedded update id", e.embedded_update_id);
    pushE("iOS bundle id in config", e.ios_bundle_identifier);
    pushE("Android package in config", e.android_package);
    pushE("Declared platforms", (e.platforms || []).join(", "));
    if (ex.length) resultBox.appendChild(section("Expo details", gridOf(ex)));
  }

  // packages
  const pkgs = r.packages || [];
  if (pkgs.length) {
    const box = el("div");
    const groups = {
      "expo-module": "Expo SDK packages",
      "unresolved-expo-module": "Expo modules we cannot name (app-owned or third-party)",
      "config-plugin": "Config plugins declared in the Expo app config",
      native: "Native React Native libraries",
      javascript: "Referenced in the JavaScript bundle",
    };
    Object.entries(groups).forEach(([kind, title]) => {
      const items = pkgs.filter((p) => p.kind === kind);
      if (!items.length) return;
      box.appendChild(el("h3", null, `${title} (${items.length})`));
      const list = el("div", "pkgs");
      items.forEach((p) => {
        const chip = el("span", "pkg " + kind, p.name);
        chip.title = p.source;
        list.appendChild(chip);
      });
      box.appendChild(list);
    });
    resultBox.appendChild(section(`Packages detected (${pkgs.length})`, box));
  }

  // evidence
  const ev = r.evidence || [];
  if (ev.length) {
    const table = el("table");
    const head = el("tr");
    ["Signal", "Matched on", "For", "Weight"].forEach((h) => head.appendChild(el("th", null, h)));
    table.appendChild(head);
    ev.forEach((item) => {
      const tr = el("tr");
      tr.appendChild(el("td", null, item.signal));
      tr.appendChild(el("td", "w", item.where));
      tr.appendChild(el("td", "w", item.framework));
      tr.appendChild(el("td", "w", String(item.weight)));
      table.appendChild(tr);
    });
    resultBox.appendChild(section(`Evidence (${ev.length} signals)`, table));
  }

  if (r.notes && r.notes.length) resultBox.appendChild(notesBlock(r.notes));
  resultBox.appendChild(permalink());
  resultBox.appendChild(rawBlock(r));
}

function renderIosOnly(r, card, store) {
  const p = r.probes || {};
  const gh = p.github || {};
  const web = p.website || {};
  const ghVerdict = gh.verdict;

  // A public repository answers the question outright; otherwise say so plainly.
  const verdicts = el("div", "verdicts");
  if (ghVerdict) {
    verdicts.appendChild(
      verdictTile(
        "React Native",
        ghVerdict.react_native ? "Yes" : "No",
        ghVerdict.react_native,
        "from " + ghVerdict.repo + " on GitHub"
      )
    );
    const t = verdictTile(
      "Expo",
      ghVerdict.expo ? (ghVerdict.expo_version ? "expo " + ghVerdict.expo_version : "Yes") : "No",
      ghVerdict.expo,
      "declared in that repository's package.json"
    );
    t.classList.add("expo");
    verdicts.appendChild(t);
    verdicts.appendChild(verdictTile("Source", "public repo", null, "read from source, not from a binary"));
  } else {
    verdicts.appendChild(verdictTile("React Native", "Unknown", null, "no binary and no public source"));
    const t = verdictTile("Expo", "Unknown", null, "no binary and no public source");
    t.classList.add("expo");
    verdicts.appendChild(t);
    verdicts.appendChild(
      verdictTile("Binary", "not available", null, "Apple publishes no downloadable app binary")
    );
  }
  card.appendChild(verdicts);
  const counterpart = r.android_counterpart;
  card.appendChild(
    el(
      "p",
      "summary",
      ghVerdict
        ? `Answered from public source: ${ghVerdict.repo}. No binary was needed.`
        : counterpart
        ? `The Android build is ${counterpart.package}, but no mirror serves it. Upload that APK and you get a full answer.`
        : "This app ships on iOS only, and its source is not public, so the stack cannot be measured. Here is every probe that ran."
    )
  );
  resultBox.appendChild(card);

  // App Store facts are still worth showing.
  const facts = [];
  const pushS = (k, v) => v && facts.push([k, String(v)]);
  pushS("Developer", store.developer);
  pushS("Bundle id", store.bundle_id);
  pushS("Version", store.version);
  pushS("Download size", store.size_bytes && (store.size_bytes / 1e6).toFixed(1) + " MB");
  pushS("Minimum iOS", store.minimum_os_version);
  pushS("Last released", (store.released || "").slice(0, 10));
  pushS("Category", (store.genres || []).join(", "));
  pushS("Developer site", store.seller_url);
  if (facts.length) resultBox.appendChild(section("App Store facts", gridOf(facts)));

  // What each probe did.
  const probeBox = el("div");
  probeBox.appendChild(el("h3", null, "Google Play search for an Android build"));
  const playList = el("ul");
  (p.play_search || ["Not run."]).forEach((line) => playList.appendChild(el("li", null, line)));
  probeBox.appendChild(playList);

  probeBox.appendChild(el("h3", null, "GitHub code search for the bundle identifier"));
  if (ghVerdict) {
    const detail = [
      `${ghVerdict.repo} declares this bundle id.`,
      ghVerdict.expo ? `expo ${ghVerdict.expo_version || ""}`.trim() : "no expo dependency",
      ghVerdict.react_native ? `react-native ${ghVerdict.react_native_version || ""}`.trim() : "no react-native dependency",
      ghVerdict.expo_router ? "expo-router" : null,
    ].filter(Boolean);
    probeBox.appendChild(el("p", null, detail.join(" · ")));
  } else if (gh.error) {
    probeBox.appendChild(el("p", "muted", gh.error));
  } else {
    probeBox.appendChild(
      el("p", "muted", `No public repository declares ${store.bundle_id || "this bundle id"} (${gh.total || 0} code hits).`)
    );
  }

  probeBox.appendChild(el("h3", null, "Developer website fingerprint"));
  if (web.hits && web.hits.length) {
    probeBox.appendChild(
      el("p", null, `${web.url} is built with: ${web.hits.join(", ")}. That is the same toolchain family as the app, but it is evidence about their website, not about this binary.`)
    );
  } else if (web.error) {
    probeBox.appendChild(el("p", "muted", `${web.url || "No site listed"} — ${web.error}`));
  } else if (web.ran) {
    probeBox.appendChild(
      el("p", "muted", `No React Native or Expo output found on ${web.url}. Their marketing site says nothing about the app.`)
    );
  } else {
    probeBox.appendChild(el("p", "muted", "The App Store listing gives no developer website."));
  }
  resultBox.appendChild(section("Probes that ran", probeBox));

  if (r.next_steps && r.next_steps.length) {
    const box = el("div");
    const list = el("ol");
    r.next_steps.forEach((s) => list.appendChild(el("li", null, s)));
    box.appendChild(list);
    resultBox.appendChild(section("How to get a real answer", box));
  }

  if (r.notes && r.notes.length) resultBox.appendChild(notesBlock(r.notes));
  resultBox.appendChild(permalink());
  resultBox.appendChild(rawBlock(r));
}

function permalink() {
  const box = el("p", "muted");
  if (!CURRENT_JOB) return box;
  box.appendChild(document.createTextNode("Shareable link to this report: "));
  const a = el("a", null, `${window.location.origin}/j/${CURRENT_JOB}`);
  a.href = `/j/${CURRENT_JOB}`;
  box.appendChild(a);
  
  return box;
}

function verdictTile(label, value, state, note) {
  const cls = state === true ? "verdict yes" : state === false ? "verdict no" : "verdict";
  const tile = el("div", cls);
  tile.appendChild(el("div", "label", label));
  tile.appendChild(el("div", "value", value));
  if (note) tile.appendChild(el("div", "note", note));
  return tile;
}

function section(title, node) {
  const wrap = el("div");
  wrap.appendChild(el("h2", null, title));
  const card = el("div", "card");
  card.appendChild(node);
  wrap.appendChild(card);
  return wrap;
}

function gridOf(pairs) {
  const grid = el("div", "grid");
  pairs.forEach(([k, v]) => {
    const f = el("div", "fact");
    f.appendChild(el("div", "k", k));
    f.appendChild(el("div", "v", v));
    grid.appendChild(f);
  });
  return grid;
}

function notesBlock(notes) {
  const box = el("div", "notes");
  notes.forEach((n) => box.appendChild(el("p", null, n)));
  return box;
}

function rawBlock(r) {
  const d = el("details");
  d.appendChild(el("summary", null, "Raw JSON"));
  const pre = el("pre", null, JSON.stringify(r, null, 2));
  d.appendChild(pre);
  return d;
}

let HEALTH = {};

fetch("/api/health")
  .then((r) => r.json())
  .then((h) => {
    HEALTH = h;
    const origin = window.location.origin;
    $("#snippet-script").textContent =
      `curl -O ${origin}/static/get-ipa.sh && bash get-ipa.sh \\\n` +
      `  https://apps.apple.com/us/app/id6444370199`;
    $("#snippet-curl").textContent = `curl -F file=@YourApp.ipa ${origin}/api/jobs/upload`;

    const ios = h.ios_download || {};
    const status = $("#ios-status");
    if (ios.signed_in) {
      status.textContent = `This server is signed in to the App Store as ${ios.account || "an Apple ID"}, so an App Store link is downloaded and scanned automatically — no upload needed.`;
      status.className = "";
    } else if (ios.available) {
      status.innerHTML =
        "This server has <code>ipatool</code> but no Apple ID signed in, so iOS apps cannot be downloaded here yet. " +
        "Use the command below from your Mac, or sign in once on the server with <code>bin/ipatool auth login</code>.";
      status.className = "muted";
    } else {
      status.textContent = "iOS downloads are not configured on this server. Upload an .ipa, or use the command below.";
      status.className = "muted";
    }

    $("#footnote").textContent = h.apk_download
      ? `Android APKs are downloaded automatically from ${h.source}.`
      : "APK downloading is off on this server. Upload a file to analyze it.";
  });

// A /j/<id> link opens a finished analysis.
const shared = window.location.pathname.match(/^\/j\/([a-z0-9]+)$/i);
if (shared) {
  reset("Loading saved analysis " + shared[1]);
  watch(shared[1]);
}
