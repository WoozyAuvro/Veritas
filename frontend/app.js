const statusPanel = document.getElementById('status-panel');
const resultsPanel = document.getElementById('results-panel');
const progressLog = document.getElementById('progress-log');
const resultsSummary = document.getElementById('results-summary');
const resultsLogs = document.getElementById('results-logs');
const statusPill = document.getElementById('status-pill');

const bankInput = document.getElementById('bank-statement');
const emailInput = document.getElementById('emails');
const receiptInput = document.getElementById('receipts');

const bankLabel = document.getElementById('bank-file-label');
const emailLabel = document.getElementById('email-file-label');
const receiptLabel = document.getElementById('receipt-file-label');

const runButton = document.getElementById('run-btn');
const demoButton = document.getElementById('demo-btn');

function setStatus(text) {
  statusPill.textContent = text;
}

function appendLog(message) {
  const item = document.createElement('div');
  item.className = 'log-item';
  item.textContent = message;
  progressLog.appendChild(item);
}

function hydrateFileLabels() {
  bankInput.addEventListener('change', () => {
    bankLabel.textContent = bankInput.files?.[0]?.name || 'Choose a CSV file';
  });
  emailInput.addEventListener('change', () => {
    const names = Array.from(emailInput.files || []).map((file) => file.name);
    emailLabel.textContent = names.length ? names.join(', ') : 'Choose one or more .eml/.txt files';
  });
  receiptInput.addEventListener('change', () => {
    const names = Array.from(receiptInput.files || []).map((file) => file.name);
    receiptLabel.textContent = names.length ? names.join(', ') : 'Choose one or more .pdf/.txt files';
  });
}

async function postPipeline(useDemo) {
  statusPanel.classList.remove('hidden');
  resultsPanel.classList.add('hidden');
  progressLog.innerHTML = '';
  resultsSummary.innerHTML = '';
  resultsLogs.innerHTML = '';
  setStatus('Running');
  appendLog('Submitting pipeline request...');

  const formData = new FormData();
  formData.append('use_demo', String(useDemo));

  if (!useDemo) {
    if (bankInput.files?.[0]) {
      formData.append('bank_statement', bankInput.files[0]);
    }
    if (emailInput.files?.length) {
      Array.from(emailInput.files).forEach((file) => formData.append('emails', file));
    }
    if (receiptInput.files?.length) {
      Array.from(receiptInput.files).forEach((file) => formData.append('receipts', file));
    }
  }

  const response = await fetch('/api/run-pipeline', { method: 'POST', body: formData });
  const data = await response.json();
  appendLog(`Pipeline started with job id ${data.job_id}`);
  pollJob(data.job_id);
}

async function pollJob(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  const data = await response.json();
  if (data.logs?.length) {
    data.logs.forEach((log) => {
      if (!progressLog.textContent.includes(log)) {
        appendLog(log);
      }
    });
  }

  if (data.status === 'completed') {
    setStatus('Completed');
    renderResults(data);
    return;
  }

  if (data.status === 'failed') {
    setStatus('Failed');
    appendLog(data.error || 'Pipeline failed');
    return;
  }

  setTimeout(() => pollJob(jobId), 1200);
}

function renderResults(data) {
  resultsPanel.classList.remove('hidden');
  const flags = data.result?.flags || [];
  const summary = document.createElement('div');
  summary.className = 'summary-card';
  summary.innerHTML = `<strong>${flags.length}</strong> suspicious transaction(s) found.`;
  resultsSummary.appendChild(summary);

  if (data.result?.agent_report) {
    const agentCard = document.createElement('div');
    agentCard.className = 'summary-card';
    agentCard.innerHTML = `<strong>Agent report:</strong><br/>${String(data.result.agent_report).slice(0, 800)}`;
    resultsSummary.appendChild(agentCard);
  }

  if (flags.length) {
    const list = document.createElement('div');
    list.className = 'log-list';
    flags.slice(0, 5).forEach((flag) => {
      const item = document.createElement('div');
      item.className = 'log-item';
      item.textContent = `${flag.method || 'flag'} :: ${flag.vendor_name || 'unknown'} :: ${flag.reason || ''}`;
      list.appendChild(item);
    });
    resultsLogs.appendChild(list);
  }
}

runButton.addEventListener('click', () => postPipeline(false));
demoButton.addEventListener('click', () => postPipeline(true));
hydrateFileLabels();
