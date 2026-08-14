// Circuit detail page's utilization history chart — inline SVG, no chart
// library (this app has no frontend build step on purpose). Fetches
// /circuits/<id>/utilization-history?window=24h|7d and draws a single
// measure (utilization %) as two lines sharing one hue: solid = average,
// dotted = peak — differentiated by dash pattern rather than a second
// color, since both are the same underlying quantity, not two identities.
// The optional threshold is a distinct dashed reference line in the
// app's warning color, never confused with the data series. Colors reuse
// this app's existing Bootstrap 5 palette (map.js's CIRCUIT_COLOR etc.)
// rather than introducing a new one. App has no dark mode (base.html
// hardcodes data-bs-theme="light"), so this only targets light.
(() => {
  const COLOR_AVG = "#0d6efd";       // Bootstrap primary — same as map.js's "up"/CIRCUIT_COLOR blue family
  // Bootstrap 5.3's own --bs-warning-text-emphasis, not raw --bs-warning
  // (#ffc107): validated with the dataviz skill's contrast checker against
  // this app's light surface — raw #ffc107 fails contrast (1.59:1, needs
  // 3:1) as a thin line/text color, which is exactly why Bootstrap ships
  // this darker emphasis variant for that use case.
  const COLOR_THRESHOLD = "#997404";
  const COLOR_GRID = "#dee2e6";
  const COLOR_INK_MUTED = "#6c757d";
  const COLOR_CROSSHAIR = "#adb5bd";

  const container = document.getElementById("util-history");
  if (!container) return;

  const circuitId = container.dataset.circuitId;
  const thresholdPct = container.dataset.threshold ? Number(container.dataset.threshold) : null;
  const chartEl = document.getElementById("util-chart-container");
  const emptyEl = document.getElementById("util-empty");
  const tableEl = document.getElementById("util-table");
  const tableBody = document.getElementById("util-table-body");
  const tableToggle = document.getElementById("util-table-toggle");

  let currentPoints = [];
  let currentWindowKind = "24h";
  let tableShown = false;

  // H is fixed (chart height doesn't need to track container width), but W
  // is measured per-render from the container's actual rendered width — see
  // the comment in render() for why a fixed W here would leave the chart
  // stuck narrower than its container.
  const H = 160, PAD_L = 34, PAD_R = 12, PAD_T = 10, PAD_B = 22;
  const plotH = H - PAD_T - PAD_B;

  function fmtTime(iso, windowKind) {
    const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : "Z"));
    return windowKind === "7d"
      ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
      : d.toLocaleTimeString(undefined, { hour: "numeric" });
  }

  function svgEl(tag, attrs) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  }

  function render(points, windowKind) {
    currentPoints = points;
    currentWindowKind = windowKind;
    chartEl.innerHTML = "";
    if (!points.length) {
      emptyEl.classList.remove("d-none");
      return;
    }
    emptyEl.classList.add("d-none");

    // viewBox width is measured from the container, not a fixed constant —
    // width="100%" + a fixed height means the browser's default
    // preserveAspectRatio scales the viewBox uniformly to fit the *height*
    // (whose scale factor is always 1, since height is always exactly H),
    // then centers the result. With a fixed viewBox width that pins the
    // actual drawing at that many CSS pixels wide forever, regardless of
    // how wide the container really is — the svg element fills the column,
    // the chart inside it doesn't. Matching W to clientWidth keeps the
    // scale factor 1:1 on both axes so the plotted lines actually reach
    // the container's edges.
    const W = Math.max(320, Math.round(chartEl.clientWidth)) || 640;
    const plotW = W - PAD_L - PAD_R;

    // Headroom above 100% if a peak actually exceeded capacity — real,
    // worth showing rather than clipping at the top of the chart.
    const maxVal = Math.max(100, thresholdPct || 0, ...points.map((p) => p.peak_pct));
    const yMax = Math.ceil(maxVal / 10) * 10;
    const x = (i) => PAD_L + (points.length === 1 ? plotW / 2 : (i / (points.length - 1)) * plotW);
    const y = (v) => PAD_T + plotH - (v / yMax) * plotH;

    const svg = svgEl("svg", {
      viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img",
      "aria-label": "Utilization history chart",
    });

    // Gridlines + y labels (0 / half / max) — recessive, thin, muted ink.
    [0, yMax / 2, yMax].forEach((v) => {
      svg.appendChild(svgEl("line", {
        x1: PAD_L, x2: W - PAD_R, y1: y(v), y2: y(v), stroke: COLOR_GRID, "stroke-width": 1,
      }));
      const label = svgEl("text", {
        x: PAD_L - 6, y: y(v) + 3, "text-anchor": "end", "font-size": 10, fill: COLOR_INK_MUTED,
      });
      label.textContent = `${v}%`;
      svg.appendChild(label);
    });

    // A few x labels — first, middle, last — not one per point (too dense).
    [0, Math.floor((points.length - 1) / 2), points.length - 1].forEach((i) => {
      const label = svgEl("text", {
        x: x(i), y: H - 6, "text-anchor": i === 0 ? "start" : i === points.length - 1 ? "end" : "middle",
        "font-size": 10, fill: COLOR_INK_MUTED,
      });
      label.textContent = fmtTime(points[i].period_start, windowKind);
      svg.appendChild(label);
    });

    if (thresholdPct != null) {
      svg.appendChild(svgEl("line", {
        x1: PAD_L, x2: W - PAD_R, y1: y(thresholdPct), y2: y(thresholdPct),
        stroke: COLOR_THRESHOLD, "stroke-width": 1.5, "stroke-dasharray": "6,4",
      }));
    }

    function pathFor(key) {
      return points.map((p, i) => `${i === 0 ? "M" : "L"} ${x(i).toFixed(1)} ${y(p[key]).toFixed(1)}`).join(" ");
    }
    svg.appendChild(svgEl("path", {
      d: pathFor("peak_pct"), fill: "none", stroke: COLOR_AVG, "stroke-width": 1.5,
      "stroke-dasharray": "2,3", "stroke-linecap": "round",
    }));
    svg.appendChild(svgEl("path", {
      d: pathFor("avg_pct"), fill: "none", stroke: COLOR_AVG, "stroke-width": 2, "stroke-linecap": "round",
    }));

    // Hover layer: one invisible full-height rect per point, wider than
    // the mark itself so the hit target isn't pixel-thin.
    const crosshair = svgEl("line", {
      x1: 0, x2: 0, y1: PAD_T, y2: PAD_T + plotH, stroke: COLOR_CROSSHAIR, "stroke-width": 1, opacity: 0,
    });
    const dotAvg = svgEl("circle", { r: 3, fill: COLOR_AVG, opacity: 0 });
    const dotPeak = svgEl("circle", { r: 3, fill: "#fff", stroke: COLOR_AVG, "stroke-width": 1.5, opacity: 0 });
    svg.append(crosshair, dotAvg, dotPeak);

    const tooltip = document.createElement("div");
    tooltip.className = "small border rounded bg-body shadow-sm px-2 py-1";
    tooltip.style.cssText = "position:absolute; pointer-events:none; display:none; white-space:nowrap; z-index:5;";
    chartEl.style.position = "relative";

    const hitWidth = points.length > 1 ? plotW / (points.length - 1) : plotW;
    points.forEach((p, i) => {
      const hit = svgEl("rect", {
        x: x(i) - hitWidth / 2, y: PAD_T, width: hitWidth, height: plotH, fill: "transparent",
      });
      hit.addEventListener("mouseenter", () => {
        crosshair.setAttribute("x1", x(i)); crosshair.setAttribute("x2", x(i)); crosshair.setAttribute("opacity", 1);
        dotAvg.setAttribute("cx", x(i)); dotAvg.setAttribute("cy", y(p.avg_pct)); dotAvg.setAttribute("opacity", 1);
        dotPeak.setAttribute("cx", x(i)); dotPeak.setAttribute("cy", y(p.peak_pct)); dotPeak.setAttribute("opacity", 1);
        tooltip.innerHTML = `<strong>${fmtTime(p.period_start, windowKind)}</strong><br>Avg: ${p.avg_pct}%<br>Peak: ${p.peak_pct}%`;
        tooltip.style.display = "block";
        const left = Math.min(Math.max((x(i) / W) * chartEl.clientWidth - 40, 0), chartEl.clientWidth - 100);
        tooltip.style.left = `${left}px`;
        tooltip.style.top = "0px";
      });
      hit.addEventListener("mouseleave", () => {
        crosshair.setAttribute("opacity", 0);
        dotAvg.setAttribute("opacity", 0);
        dotPeak.setAttribute("opacity", 0);
        tooltip.style.display = "none";
      });
      svg.appendChild(hit);
    });

    chartEl.appendChild(svg);
    chartEl.appendChild(tooltip);

    // Legend — plain text, not SVG, so it stays selectable/readable by
    // assistive tech; identity comes from the swatch shape, not color
    // alone, since avg/peak share a hue on purpose.
    const legend = document.createElement("div");
    legend.className = "small text-body-secondary mt-1";
    legend.innerHTML = `<span style="color:${COLOR_AVG}">&#9473;</span> Avg` +
      `&nbsp;&nbsp;<span style="color:${COLOR_AVG}">&#8231;&#8231;&#8231;</span> Peak` +
      (thresholdPct != null ? `&nbsp;&nbsp;<span style="color:${COLOR_THRESHOLD}">&#8213;&#8213;</span> Threshold (${thresholdPct}%)` : "");
    chartEl.appendChild(legend);

    if (tableShown) renderTable();
  }

  function renderTable() {
    tableBody.innerHTML = "";
    currentPoints.forEach((p) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${p.period_start.replace("T", " ").slice(0, 16)}</td><td>${p.avg_pct}%</td><td>${p.peak_pct}%</td>`;
      tableBody.appendChild(tr);
    });
  }

  function load(windowKind) {
    fetch(`/circuits/${circuitId}/utilization-history?window=${windowKind}`)
      .then((r) => r.json())
      .then((data) => render(data.points, windowKind));
  }

  container.querySelectorAll("[data-window]").forEach((btn) => {
    btn.addEventListener("click", () => {
      container.querySelectorAll("[data-window]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      load(btn.dataset.window);
    });
  });

  if (tableToggle) {
    tableToggle.addEventListener("click", (e) => {
      e.preventDefault();
      tableShown = !tableShown;
      tableEl.classList.toggle("d-none", !tableShown);
      tableToggle.textContent = tableShown ? "Hide table" : "View as table";
      if (tableShown) renderTable();
    });
  }

  // Re-render (no refetch — same points already in hand) when the
  // container's width actually changes, so W stays matched to it instead
  // of going stale until the next window-toggle click.
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (currentPoints.length) render(currentPoints, currentWindowKind);
    }, 150);
  });

  load("24h");
})();
