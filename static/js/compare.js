(function () {
  const msToS = (ms) => (ms || 0) / 1000;
  const colorPalette = [
    '#2a5298', '#f39c12', '#27ae60', '#8e44ad', '#e74c3c', '#16a085', '#c0392b'
  ];

  const state = {
    transactions: (window.VP_DATA && window.VP_DATA.transactions) || [],
    comparisons: (window.VP_DATA && window.VP_DATA.comparisons) || [],
    selectedTxns: new Set(),
    metric: 'Avg (ms)',
    charts: {
      avg: null,
      error: null,
      rag: null
    },
    ragTimer: null
  };

  function initSelections() {
    if (!state.transactions.length) return;
    state.selectedTxns = new Set(state.transactions); // all selected by default
    const txnFilter = document.getElementById('txnFilter');
    const metricSelect = document.getElementById('metricSelect');

    metricSelect.addEventListener('change', () => {
      state.metric = metricSelect.value;
    });

    // Keep selected set updated
    txnFilter.addEventListener('change', () => {
      state.selectedTxns.clear();
      Array.from(txnFilter.selectedOptions).forEach(opt => state.selectedTxns.add(opt.value));
    });
  }

  function datasetsForMetric(metric, convertToSeconds) {
    return state.comparisons.map((rep, idx) => {
      const data = state.transactions.map(txn => {
        if (!state.selectedTxns.has(txn)) return null;
        const v = (rep.metricsByTxn[txn] && rep.metricsByTxn[txn][metric]) || 0;
        return convertToSeconds ? msToS(v) : v;
      });
      return {
        label: rep.name,
        data,
        borderColor: colorPalette[idx % colorPalette.length],
        backgroundColor: colorPalette[idx % colorPalette.length] + '33',
        borderWidth: 2,
        spanGaps: true
      };
    });
  }

  function buildChart(ctxId, type, labels, datasets, title) {
    const ctx = document.getElementById(ctxId).getContext('2d');
    if (!ctx) return null;
    const existing = Chart.getChart(ctxId);
    if (existing) existing.destroy();

    return new Chart(ctx, {
      type,
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          y: { beginAtZero: true }
        },
        plugins: {
          legend: { position: 'top' },
          title: { display: true, text: title }
        }
      }
    });
  }

  function updateCharts() {
    const labels = state.transactions.map(txn => state.selectedTxns.has(txn) ? txn : '');
    const avgDatasets = datasetsForMetric('Avg (ms)', true);
    const errDatasets = datasetsForMetric('Error %', false);

    state.charts.avg = buildChart('avgChart', 'line', labels, avgDatasets, 'Avg Response Time (s)');
    state.charts.error = buildChart('errorChart', 'line', labels, errDatasets, 'Error Percentage (%)');
  }

  // RAG encoding: GREEN=2, AMBER=1, RED=0, UNKNOWN=-1
  function ragValue(r) {
    if (r === 'GREEN') return 2;
    if (r === 'AMBER') return 1;
    if (r === 'RED') return 0;
    return -1;
  }

  function updateRagChartForTxn(txn) {
    const labels = state.comparisons.map(rep => rep.name);
    const data = state.comparisons.map(rep => {
      const row = rep.metricsByTxn[txn] || {};
      return ragValue(row['RAG']);
    });

    const colors = data.map(v => {
      if (v === 2) return '#27ae60';
      if (v === 1) return '#f39c12';
      if (v === 0) return '#e74c3c';
      return '#7f8c8d';
    });

    const datasets = [{
      label: `RAG for ${txn}`,
      data,
      backgroundColor: colors
    }];

    state.charts.rag = buildChart('ragChart', 'bar', labels, datasets, `RAG Drift — ${txn}`);
    if (state.charts.rag) {
      state.charts.rag.options.scales.y = {
        ticks: {
          stepSize: 1,
          callback: (val) => ({0: 'RED', 1: 'AMBER', 2: 'GREEN'}[val] || ''
        },
        min: 0, max: 2
      };
      state.charts.rag.update();
    }
  }

  function playRag() {
    const sel = document.getElementById('ragTxnSelect');
    let idx = sel.selectedIndex >= 0 ? sel.selectedIndex : 0;
    clearInterval(state.ragTimer);
    state.ragTimer = setInterval(() => {
      sel.selectedIndex = idx % sel.options.length;
      updateRagChartForTxn(sel.value);
      idx += 1;
    }, 1500);
  }

  function pauseRag() {
    clearInterval(state.ragTimer);
    state.ragTimer = null;
  }

  // Table <-> Graph toggle
  function showTable() {
    document.getElementById('tableView').style.display = 'block';
    document.getElementById('graphView').style.display = 'none';
  }
  function showGraph() {
    document.getElementById('tableView').style.display = 'none';
    document.getElementById('graphView').style.display = 'block';
    // Lazy init/update
    updateCharts();
    const sel = document.getElementById('ragTxnSelect');
    updateRagChartForTxn(sel.value);
  }

  // Export canvas to PNG
  function downloadCanvas(canvasId, filename) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const link = document.createElement('a');
    link.download = filename || 'chart.png';
    link.href = canvas.toDataURL('image/png');
    link.click();
  }

  // Expose public API
  window.VP = {
    updateCharts,
    showTable,
    showGraph,
    playRag,
    pauseRag,
    downloadCanvas
  };

  // Init
  document.addEventListener('DOMContentLoaded', () => {
    initSelections();

    // If user switches metric and applies filters:
    document.getElementById('metricSelect').addEventListener('change', () => {
      // When metric changes, we’ll update the "avg chart" to reflect chosen metric
      // by swapping data dimension from Avg to selected metric.
      // Rewire avg chart dataset to chosen metric on next Apply Filters.
    });

    // First render table view visible by default
    showTable();
  });
})();