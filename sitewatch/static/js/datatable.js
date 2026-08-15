// Progressively enhances any <div class="datatable"> wrapping a <table>
// with search, sortable headers, and pagination (List.js). List.js reuses
// the actual <tr> DOM nodes it finds already in the tbody — it only
// reorders/shows/hides them, it never rebuilds row HTML — so data-href
// (clickable_rows.js), badges, edit links, and inline forms/buttons in a
// row keep working with zero extra wiring.
//
// Per-table markup contract:
//   <div class="datatable" id="unique-id" data-page-size="25">
//     <table>
//       <thead><tr>
//         <th data-sort="fieldname">Label</th>                 <!-- text column: sorts/searches on the innerHTML of whatever inside the <td> carries class="fieldname" -->
//         <th data-sort="fieldname" data-sort-attr="data-value">Label</th>  <!-- badge/numeric column: put class="fieldname" data-value="rawvalue" on the <td> itself so sort/search use the raw value, not the badge markup -->
//         <th></th>                                             <!-- no data-sort: not indexed, not clickable-to-sort (e.g. an actions column) -->
//       </tr></thead>
//       <tbody>...</tbody>
//     </table>
//   </div>
// A table with zero rows (empty state) is left alone. Every table starts
// sorted ascending by its first sortable column, UNLESS the container
// carries data-default-sort="fieldname" (and optionally data-default-
// sort-dir="desc", ascending otherwise) — e.g. a "Down since" history
// table wants newest-first, not chronological-first.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".datatable").forEach((container) => {
    const table = container.querySelector("table");
    const tbody = table.querySelector("tbody");
    if (!tbody || !tbody.children.length) return;

    tbody.classList.add("datatable-body");

    const sortHeaders = Array.from(table.querySelectorAll("thead [data-sort]"));
    sortHeaders.forEach((th) => th.classList.add("sort"));
    const valueNames = sortHeaders.map((th) =>
      th.dataset.sortAttr ? { attr: th.dataset.sortAttr, name: th.dataset.sort } : th.dataset.sort
    );

    const defaultPageSize = parseInt(container.dataset.pageSize || "25", 10);
    const pageSizes = [10, 25, 50, 100];

    const toolbar = document.createElement("div");
    toolbar.className = "d-flex justify-content-between align-items-center mb-2 gap-2 flex-wrap";
    toolbar.innerHTML = `
      <input type="search" class="search form-control form-control-sm" style="max-width:16rem" placeholder="Search…">
      <div class="d-flex align-items-center gap-2">
        <label class="small text-body-secondary mb-0">Show</label>
        <select class="page-size form-select form-select-sm w-auto"></select>
      </div>`;
    container.insertBefore(toolbar, table);

    const sizeSelect = toolbar.querySelector(".page-size");
    pageSizes.forEach((n) => {
      const opt = document.createElement("option");
      opt.value = String(n);
      opt.textContent = String(n);
      if (n === defaultPageSize) opt.selected = true;
      sizeSelect.appendChild(opt);
    });
    const allOpt = document.createElement("option");
    allOpt.value = "all";
    allOpt.textContent = "All";
    sizeSelect.appendChild(allOpt);

    const paginationEl = document.createElement("ul");
    paginationEl.className = "pagination pagination-sm justify-content-end mt-2 mb-0";
    container.appendChild(paginationEl);

    const list = new List(container, {
      valueNames,
      listClass: "datatable-body",
      searchClass: "search",
      sortClass: "sort",
      page: defaultPageSize,
      pagination: {
        item: "<li class='page-item'><a class='page page-link' href='#'></a></li>",
      },
    });

    sizeSelect.addEventListener("change", () => {
      const size = sizeSelect.value === "all" ? Math.max(list.size(), 1) : parseInt(sizeSelect.value, 10);
      list.show(1, size);
    });

    // Default sort: first sortable column, ascending, unless overridden —
    // every table starts in a predictable order instead of raw insertion
    // order.
    const defaultSortField = container.dataset.defaultSort || (sortHeaders[0] && sortHeaders[0].dataset.sort);
    if (defaultSortField) {
      list.sort(defaultSortField, { order: container.dataset.defaultSortDir || "asc" });
    }
  });
});
