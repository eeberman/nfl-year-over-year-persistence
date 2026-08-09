const DOMAIN_ORDER = ["individual_qb", "team_qb", "offense", "defense", "record"];
const DOMAIN_TITLES = {
  individual_qb: "Individual QB",
  team_qb: "Team QB",
  offense: "Offense",
  defense: "Defense",
  record: "Record",
};

const number = (value, digits = 3) => value == null ? "—" : Number(value).toFixed(digits);
const percent = value => value == null ? "—" : `${Math.round(Number(value) * 100)}%`;
const ci = row => row?.ci_95?.every(v => v != null) ? `[${number(row.ci_95[0])}, ${number(row.ci_95[1])}]` : "—";
const esc = value => String(value).replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));

function element(id) { return document.getElementById(id); }

function renderSummary(data) {
  const table = element("headline-table");
  if (!table) return;
  const rows = DOMAIN_ORDER.map(id => {
    const domain = data.domains[id];
    const cells = [1, 2, 5].map(window => {
      const stats = domain.windows[String(window)];
      return `<td class="numeric"><strong>${number(stats.pearson_r)}</strong><br><span class="muted">${ci(stats)} · n=${stats.n_pairs}</span></td>`;
    }).join("");
    return `<tr><td><a href="${id}.html">${DOMAIN_TITLES[id]}</a></td>${cells}</tr>`;
  }).join("");
  table.innerHTML = `<div class="table-scroll"><table><thead><tr><th>Performance type</th><th>Prior 1 year</th><th>Prior 2-year average</th><th>Prior 5-year average</th></tr></thead><tbody>${rows}</tbody></table></div><p class="table-note">Cells show Pearson r, 95% cluster-bootstrap CI, and pair count. Higher values mean more persistence.</p>`;
  renderIntervalChart(data);
}

function renderIntervalChart(data) {
  const holder = element("interval-chart");
  if (!holder) return;
  const items = [];
  DOMAIN_ORDER.forEach(domainId => [1, 2, 5].forEach(window => {
    const s = data.domains[domainId].windows[String(window)];
    items.push({ domainId, window, ...s });
  }));
  const width = 860, left = 205, right = 34, top = 38, row = 29, height = top + items.length * row + 38;
  const x = value => left + ((value + 1) / 2) * (width - left - right);
  const grid = [-1, -.5, 0, .5, 1].map(value => `<line class="chart-grid" x1="${x(value)}" y1="${top - 16}" x2="${x(value)}" y2="${height - 25}"/><text class="chart-label" x="${x(value)}" y="${height - 8}" text-anchor="middle">${value.toFixed(1)}</text>`).join("");
  const rows = items.map((item, index) => {
    const y = top + index * row;
    const label = `${DOMAIN_TITLES[item.domainId]} · ${item.window}y`;
    const low = item.ci_95[0], high = item.ci_95[1];
    return `<text class="chart-label" x="${left - 12}" y="${y + 4}" text-anchor="end">${esc(label)}</text><line class="ci-line" x1="${x(low)}" y1="${y}" x2="${x(high)}" y2="${y}"/><circle class="ci-dot" cx="${x(item.pearson_r)}" cy="${y}" r="5"><title>${esc(label)}: r ${number(item.pearson_r)}, 95% CI ${ci(item)}</title></circle>`;
  }).join("");
  holder.innerHTML = `<div class="svg-wrap"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Pearson correlations and 95 percent confidence intervals by domain and history window"><text class="chart-title" x="${left}" y="18">Pearson r (higher means greater persistence)</text>${grid}${rows}</svg></div>`;
}

function renderDomain(data, domainId) {
  const domain = data.domains[domainId];
  if (!domain) return;
  element("domain-title").textContent = document.querySelector("h1")?.textContent || DOMAIN_TITLES[domainId];
  element("metric-definition").textContent = domain.metric_description;
  element("unit-description").textContent = domain.unit_description;
  const tableRows = [1, 2, 5].map(window => {
    const s = domain.windows[String(window)];
    return `<tr><td>${window}-year ${window === 1 ? "history" : "average"}</td><td class="numeric">${number(s.pearson_r)}</td><td class="numeric">${ci(s)}</td><td class="numeric">${number(s.spearman_rho)}</td><td class="numeric">${s.n_pairs}</td><td class="numeric">${s.n_entities}</td></tr>`;
  }).join("");
  element("results-table").innerHTML = `<div class="table-scroll"><table><thead><tr><th>Predictor</th><th>Pearson r</th><th>95% CI</th><th>Spearman ρ</th><th>Pairs</th><th>Entities</th></tr></thead><tbody>${tableRows}</tbody></table></div>`;
  const notes = element("domain-notes");
  if (notes) notes.innerHTML = domain.notes.map(note => `<li>${esc(note)}</li>`).join("");
  if (domain.qualification) {
    element("qualification").innerHTML = `<div class="callout"><strong>QB sample</strong><p>${esc(domain.qualification.headline_rule)} ${domain.qualification.qualifying_qb_seasons} qualifying QB-seasons from ${domain.qualification.qualifying_qbs} quarterbacks.</p></div>`;
  }
  const controls = element("window-controls");
  controls.innerHTML = [1, 2, 5].map(window => `<button type="button" data-window="${window}" aria-pressed="${window === 1}">Prior ${window === 1 ? "year" : `${window}-year average`}</button>`).join("");
  controls.querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
    controls.querySelectorAll("button").forEach(other => other.setAttribute("aria-pressed", String(other === button)));
    renderScatter(domain, Number(button.dataset.window));
  }));
  renderScatter(domain, 1);
  renderRobustness(domain);
}

function niceRange(values) {
  const min = Math.min(...values), max = Math.max(...values);
  const spread = Math.max(max - min, Math.abs(max) * .12, .02);
  return [min - spread * .12, max + spread * .12];
}

function renderScatter(domain, window) {
  const stats = domain.windows[String(window)];
  const points = stats.points;
  const holder = element("scatter-chart");
  const context = element("scatter-context");
  context.textContent = `Prior ${window === 1 ? "season" : `${window}-season equally weighted average`} → next season. Pearson r = ${number(stats.pearson_r)} (95% CI ${ci(stats)}; n = ${stats.n_pairs}).`;
  if (!points.length) { holder.innerHTML = "<p class='muted'>No eligible pairs for this window.</p>"; return; }
  const width = 860, height = 530, margin = { left: 76, right: 28, top: 35, bottom: 75 };
  const xr = niceRange(points.map(p => p.predictor));
  const yr = niceRange(points.map(p => p.outcome));
  const x = value => margin.left + ((value - xr[0]) / (xr[1] - xr[0])) * (width - margin.left - margin.right);
  const y = value => height - margin.bottom - ((value - yr[0]) / (yr[1] - yr[0])) * (height - margin.top - margin.bottom);
  const ticks = (range, axis) => Array.from({length: 5}, (_, i) => range[0] + (i * (range[1] - range[0]) / 4)).map(value => {
    const position = axis === "x" ? x(value) : y(value);
    const line = axis === "x" ? `<line class="chart-grid" x1="${position}" y1="${margin.top}" x2="${position}" y2="${height - margin.bottom}"/>` : `<line class="chart-grid" x1="${margin.left}" y1="${position}" x2="${width - margin.right}" y2="${position}"/>`;
    const label = axis === "x" ? `<text class="chart-label" x="${position}" y="${height - margin.bottom + 20}" text-anchor="middle">${number(value)}</text>` : `<text class="chart-label" x="${margin.left - 10}" y="${position + 4}" text-anchor="end">${number(value)}</text>`;
    return line + label;
  }).join("");
  const slope = stats.regression.slope, intercept = stats.regression.intercept;
  const trend = Number.isFinite(slope) ? `<line class="chart-line" x1="${x(xr[0])}" y1="${y(slope * xr[0] + intercept)}" x2="${x(xr[1])}" y2="${y(slope * xr[1] + intercept)}"/>` : "";
  const dots = points.map(point => `<circle class="chart-point" cx="${x(point.predictor)}" cy="${y(point.outcome)}" r="4"><title>${esc(point.label)} · ${point.history_start}${point.history_start === point.history_end ? "" : `–${point.history_end}`} → ${point.target_season}\nPrior: ${number(point.predictor)} | Next: ${number(point.outcome)}</title></circle>`).join("");
  holder.innerHTML = `<div class="svg-wrap"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Scatterplot of prior performance against next-season performance"><text class="chart-title" x="${margin.left}" y="18">${esc(domain.metric_label)}</text>${ticks(xr, "x")}${ticks(yr, "y")}<line class="chart-axis" x1="${margin.left}" y1="${height - margin.bottom}" x2="${width - margin.right}" y2="${height - margin.bottom}"/><line class="chart-axis" x1="${margin.left}" y1="${margin.top}" x2="${margin.left}" y2="${height - margin.bottom}"/>${trend}${dots}<text class="chart-label" x="${(margin.left + width - margin.right) / 2}" y="${height - 20}" text-anchor="middle">Prior ${window === 1 ? "season" : `${window}-season average`}</text><text class="chart-label" transform="translate(19 ${(margin.top + height - margin.bottom) / 2}) rotate(-90)" text-anchor="middle">Following season</text><text class="chart-label" x="${width - margin.right}" y="${margin.top + 14}" text-anchor="end">Orange line: OLS trend</text></svg></div>`;
}

function renderRobustness(domain) {
  const holder = element("robustness");
  if (!holder) return;
  const blocks = [1, 2, 5].map(window => {
    const s = domain.windows[String(window)];
    return `<div><strong>${window}-year history</strong><span>Spearman ρ ${number(s.spearman_rho)} · 2020-excluded r ${number(s.excluding_2020.pearson_r)} (n=${s.excluding_2020.n_pairs}) · largest leave-one-entity shift ${number(s.leave_one_entity_out_max_delta)}</span></div>`;
  });
  if (domain.threshold_sensitivity) {
    const values = [1, 2, 5].map(w => `${w}y: r ${number(domain.threshold_sensitivity[String(w)].pearson_r)} (n=${domain.threshold_sensitivity[String(w)].n_pairs})`).join(" · ");
    blocks.push(`<div><strong>QB ≥100-dropback sensitivity</strong><span>${esc(values)}</span></div>`);
  }
  holder.innerHTML = blocks.join("");
}

async function init() {
  try {
    const response = await fetch("data/results.json");
    if (!response.ok) throw new Error(`Could not load results (${response.status}).`);
    const data = await response.json();
    renderSummary(data);
    const domainId = document.body.dataset.domain;
    if (domainId) renderDomain(data, domainId);
    document.querySelectorAll("[data-generated]").forEach(node => node.textContent = `Analysis generated ${data.generated_at_utc}`);
  } catch (error) {
    document.querySelectorAll("[data-results]").forEach(node => node.innerHTML = `<p class="error">${esc(error.message)} Run the analysis build before serving this site.</p>`);
  }
}
document.addEventListener("DOMContentLoaded", init);
