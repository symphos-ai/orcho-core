import { createApp, computed, onMounted, reactive } from "vue";
import {
  formatUserLabel,
  renderProjectSummary,
  renderTeamCard
} from "./contracts.ts";
import { SECTIONS, sectionBySlug } from "./sections.ts";

const state = reactive({
  path: window.location.pathname,
  rows: Object.create(null),
  counts: Object.create(null),
  loading: false,
  result: null
});

function navigate(path) {
  history.pushState({}, "", path);
  state.path = path;
  state.result = null;
  refresh();
}

async function fetchJson(url, options = undefined) {
  const response = await fetch(url, options);
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  return { response, payload, text };
}

async function refresh() {
  state.loading = true;
  try {
    const meta = await fetchJson("/api/meta");
    state.counts = meta.payload?.counts || {};
    for (const section of SECTIONS) {
      const result = await fetchJson(`/api/${section.slug}`);
      state.rows[section.slug] = result.payload?.items || [];
    }
  } finally {
    state.loading = false;
  }
}

async function submit(section, event) {
  event.preventDefault();
  state.result = null;
  const form = new FormData(event.currentTarget);
  const body = {};
  for (const [name] of section.fields) {
    body[name] = String(form.get(name) || "");
  }

  const { response, payload, text } = await fetchJson(`/api/${section.slug}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });

  if (response.ok && payload?.ok) {
    state.result = {
      ok: true,
      display: payload.display || renderCreated(section.slug, payload.row)
    };
    event.currentTarget.reset();
    await refresh();
  } else {
    state.result = {
      ok: false,
      status: response.status,
      error: payload?.error || { type: "Error", message: text || "Request failed" }
    };
  }
}

function renderCreated(slug, row) {
  if (slug === "users") {
    return formatUserLabel(row);
  }
  if (slug === "teams") {
    return renderTeamCard(row);
  }
  if (slug === "projects") {
    return renderProjectSummary(row);
  }
  return String(row?.pk || "");
}

function fieldOptions(type) {
  if (type === "user_lookup") {
    return (state.rows.users || []).map((user) => ({
      value: user.user_id,
      label: `${user.name} (${user.user_id})`
    }));
  }
  if (type === "team_lookup") {
    return (state.rows.teams || []).map((team) => ({
      value: team.team_id,
      label: `${team.name} (${team.team_id})`
    }));
  }
  if (type === "status_select") {
    return [
      { value: "active", label: "Active" },
      { value: "paused", label: "Paused" },
      { value: "archived", label: "Archived" }
    ];
  }
  return [];
}

function isSelectField(type) {
  return ["user_lookup", "team_lookup", "status_select"].includes(type);
}

window.addEventListener("popstate", () => {
  state.path = window.location.pathname;
  state.result = null;
  refresh();
});

const App = {
  setup() {
    onMounted(refresh);

    const route = computed(() => {
      const parts = state.path.split("/").filter(Boolean);
      const slug = parts[0] || "home";
      return {
        slug,
        mode: parts[1] === "new" ? "new" : slug === "home" ? "home" : "list",
        section: sectionBySlug(slug === "home" ? "users" : slug)
      };
    });

    return {
      SECTIONS,
      state,
      route,
      fieldOptions,
      isSelectField,
      navigate,
      submit
    };
  },
  template: `
    <header>
      <button class="brand" type="button" @click="navigate('/')">AcmeCorp</button>
      <span class="tag">User admin · Vue + TypeScript</span>
      <nav>
        <button type="button" :class="{ active: route.mode === 'home' }" @click="navigate('/')">Home</button>
        <button
          v-for="section in SECTIONS"
          :key="section.slug"
          type="button"
          :class="{ active: route.slug === section.slug }"
          @click="navigate('/' + section.slug)"
        >{{ section.label }}</button>
      </nav>
    </header>

    <main>
      <template v-if="route.mode === 'home'">
        <h1>AcmeCorp admin</h1>
        <p class="subhead">Internal tool for managing accounts, teams and projects.</p>
        <section class="tiles">
          <button
            v-for="section in SECTIONS"
            :key="section.slug"
            class="tile"
            type="button"
            @click="navigate('/' + section.slug)"
          >
            <span class="num">{{ state.counts[section.slug] ?? 0 }}</span>
            <span class="name">{{ section.label }}</span>
            <span class="hint">{{ section.title.toLowerCase() }}</span>
          </button>
        </section>
      </template>

      <template v-else-if="route.mode === 'list'">
        <section class="section-title">
          <h1>{{ route.section.label }}</h1>
          <span class="count">{{ (state.rows[route.section.slug] || []).length }} records</span>
          <button class="primary" type="button" @click="navigate('/' + route.section.slug + '/new')">
            + {{ route.section.submitLabel }}
          </button>
        </section>
        <table v-if="(state.rows[route.section.slug] || []).length">
          <thead>
            <tr>
              <th v-for="[col, label] in route.section.displayCols" :key="col">{{ label }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in state.rows[route.section.slug]" :key="row.pk">
              <td v-for="[col] in route.section.displayCols" :key="col">{{ row[col] }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">No {{ route.section.label.toLowerCase() }} yet.</div>
      </template>

      <template v-else>
        <h1>{{ route.section.title }}</h1>
        <p class="subhead">{{ route.section.blurb }}</p>
        <section class="panel">
          <form @submit="submit(route.section, $event)">
            <label v-for="[name, label, type, placeholder, defaultValue] in route.section.fields" :key="name">
              <span>{{ label }}</span>
              <select
                v-if="isSelectField(type)"
                :name="name"
                :value="defaultValue"
                required
              >
                <option
                  v-for="option in fieldOptions(type)"
                  :key="option.value"
                  :value="option.value"
                >{{ option.label }}</option>
              </select>
              <input
                v-else
                :name="name"
                :type="type"
                :placeholder="placeholder"
                :value="defaultValue"
                required
              >
            </label>
            <button class="primary" type="submit">{{ route.section.submitLabel }}</button>
          </form>

          <div v-if="state.result?.ok" class="result ok">
            <span class="muted">Created</span>
            <strong>{{ state.result.display }}</strong>
          </div>
          <div v-else-if="state.result" class="result err">
            <h2>HTTP {{ state.result.status }}</h2>
            <p>{{ state.result.error.type }}: {{ state.result.error.message }}</p>
            <pre v-if="state.result.error.traceback">{{ state.result.error.traceback }}</pre>
          </div>
        </section>
      </template>
    </main>

    <footer>AcmeCorp internal tool · demo build</footer>
  `
};

createApp(App).mount("#app");
