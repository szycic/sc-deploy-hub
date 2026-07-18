// State Management
let currentTab = "dashboard";
let servicesList = [];
let deploymentHistory = [];
let logSource = null;
let autoScroll = true;

// DOM Elements
const navDashboard = document.getElementById("nav-dashboard");
const navHistory = document.getElementById("nav-history");
const navConfig = document.getElementById("nav-config");
const tabDashboardContent = document.getElementById("tab-dashboard-content");
const tabHistoryContent = document.getElementById("tab-history-content");
const tabConfigContent = document.getElementById("tab-config-content");
const pageTitle = document.getElementById("page-title");
const pageSubtitle = document.getElementById("page-subtitle");
const btnRefresh = document.getElementById("btn-refresh");
const configTextarea = document.getElementById("config-textarea");
const configSaveStatus = document.getElementById("config-save-status");
const btnSaveConfig = document.getElementById("btn-save-config");
const btnResetConfig = document.getElementById("btn-reset-config");
const configGutterInner = document.getElementById("config-gutter-inner");
const configGutter = document.getElementById("config-gutter");
const configDirtyDot = document.getElementById("config-dirty-dot");
const configCursorPos = document.getElementById("config-cursor-pos");

let configOriginalContent = null;

// Console Modal Elements
const consoleModal = document.getElementById("console-modal");
const consoleTitle = document.getElementById("console-title");
const terminalScreen = document.getElementById("terminal-screen");
const btnCloseConsole = document.getElementById("btn-close-console");
const btnClearConsole = document.getElementById("btn-clear-console");
const btnAutoscroll = document.getElementById("btn-autoscroll");
const consoleStatusText = document.getElementById("console-status-text");

// Journal Modal Elements
const journalModal = document.getElementById("journal-modal");
const journalTitle = document.getElementById("journal-title");
const journalScreen = document.getElementById("journal-screen");
const btnCloseJournal = document.getElementById("btn-close-journal");
const btnRefreshJournal = document.getElementById("btn-refresh-journal");
const btnCloseJournalFooter = document.getElementById("btn-close-journal-footer");

let activeJournalService = null;

// Initialization
document.addEventListener("DOMContentLoaded", () => {
    setupEventListeners();
    
    // Determine active tab from URL path on first load
    const initialTab = getTabFromPath();
    switchTab(initialTab, false);

    fetchData();
    // Poll service status every 5 seconds
    setInterval(pollServices, 5000);
});

// Event Listeners Setup
function setupEventListeners() {
    // Navigation
    navDashboard.addEventListener("click", (e) => {
        e.preventDefault();
        switchTab("dashboard");
    });
    navHistory.addEventListener("click", (e) => {
        e.preventDefault();
        switchTab("history");
    });
    navConfig.addEventListener("click", (e) => {
        e.preventDefault();
        switchTab("config");
    });

    // Handle browser Back/Forward navigation
    window.addEventListener("popstate", () => {
        const tab = getTabFromPath();
        switchTab(tab, false);
    });

    // Refresh
    btnRefresh.addEventListener("click", () => {
        fetchData();
    });

    btnSaveConfig.addEventListener("click", saveConfig);
    btnResetConfig.addEventListener("click", loadConfig);

    configTextarea.addEventListener("input", () => {
        updateGutter();
        updateDirtyState();
        updateCursorPos();
    });

    configTextarea.addEventListener("keydown", (e) => {
        if (e.key === "Tab") {
            e.preventDefault();
            const start = configTextarea.selectionStart;
            const end = configTextarea.selectionEnd;
            configTextarea.value = configTextarea.value.substring(0, start) + "  " + configTextarea.value.substring(end);
            configTextarea.selectionStart = configTextarea.selectionEnd = start + 2;
            updateGutter();
            updateDirtyState();
        }
        if ((e.ctrlKey || e.metaKey) && e.key === "s") {
            e.preventDefault();
            saveConfig();
        }
    });

    configTextarea.addEventListener("click", updateCursorPos);
    configTextarea.addEventListener("keyup", updateCursorPos);

    configTextarea.addEventListener("scroll", () => {
        configGutter.scrollTop = configTextarea.scrollTop;
    });

    // Console Modal Control
    btnCloseConsole.addEventListener("click", closeConsole);
    btnClearConsole.addEventListener("click", () => {
        terminalScreen.textContent = "";
    });
    btnAutoscroll.addEventListener("click", () => {
        autoScroll = !autoScroll;
        btnAutoscroll.textContent = `Auto-Scroll: ${autoScroll ? "ON" : "OFF"}`;
        if (autoScroll) {
            terminalScreen.scrollTop = terminalScreen.scrollHeight;
        }
    });

    // Journal Modal Control
    const closeJournalModal = () => {
        journalModal.classList.add("hidden");
        activeJournalService = null;
    };
    btnCloseJournal.addEventListener("click", closeJournalModal);
    btnCloseJournalFooter.addEventListener("click", closeJournalModal);
    btnRefreshJournal.addEventListener("click", () => {
        if (activeJournalService) {
            showJournalLogs(activeJournalService);
        }
    });
}

// Helpers for Client Routing
function getTabFromPath() {
    const path = window.location.pathname;
    if (path === "/history") return "history";
    if (path === "/config") return "config";
    return "dashboard";
}

// Tab Switcher
function switchTab(tab, updateHistory = true) {
    currentTab = tab;
    const allNavItems = [navDashboard, navHistory, navConfig];
    const allTabs = [tabDashboardContent, tabHistoryContent, tabConfigContent];
    allNavItems.forEach(n => n.classList.remove("active"));
    allTabs.forEach(t => t.classList.add("hidden"));

    if (tab === "dashboard") {
        navDashboard.classList.add("active");
        tabDashboardContent.classList.remove("hidden");
        pageTitle.textContent = "Service Dashboard";
        pageSubtitle.textContent = "Monitor and orchestrate systemd deployments";
        if (updateHistory) {
            history.pushState({ tab }, "", "/dashboard");
        }
    } else if (tab === "history") {
        navHistory.classList.add("active");
        tabHistoryContent.classList.remove("hidden");
        pageTitle.textContent = "Deployment History";
        pageSubtitle.textContent = "Audit trail of webhook and manual runs";
        fetchHistory();
        if (updateHistory) {
            history.pushState({ tab }, "", "/history");
        }
    } else if (tab === "config") {
        navConfig.classList.add("active");
        tabConfigContent.classList.remove("hidden");
        pageTitle.textContent = "Configuration Editor";
        pageSubtitle.textContent = "Edit config.yaml directly from the dashboard";
        loadConfig();
        if (updateHistory) {
            history.pushState({ tab }, "", "/config");
        }
    }
}

// Data Fetch Orchestration
async function fetchData() {
    await pollServices();
    await fetchHistory();
    updateMetrics();
}

async function pollServices() {
    try {
        const response = await fetch("/api/v1/services");
        if (!response.ok) throw new Error("Offline");
        servicesList = await response.json();
        renderServices();
    } catch (error) {
        console.error("Error fetching services:", error);
    }
}

async function fetchHistory() {
    try {
        const response = await fetch("/api/v1/deployments");
        if (!response.ok) throw new Error("Offline");
        deploymentHistory = await response.json();
        renderHistory();
        updateMetrics();
    } catch (error) {
        console.error("Error fetching history:", error);
    }
}

// Update Metric Counters
function updateMetrics() {
    document.getElementById("metric-services-count").textContent = servicesList.length;
    
    const activeCount = servicesList.filter(s => s.status === "active").length;
    document.getElementById("metric-active-count").textContent = activeCount;

    // Last 24 Hours Deploys
    const now = new Date();
    const past24h = new Date(now.getTime() - 24 * 60 * 60 * 1000);
    const recentDeploysCount = deploymentHistory.filter(dep => {
        const depTime = new Date(dep.started_at);
        return depTime >= past24h;
    }).length;

    document.getElementById("metric-deploys-count").textContent = recentDeploysCount;
    document.getElementById("history-count").textContent = `${deploymentHistory.length} executions`;
}

// Render Services Grid
function renderServices() {
    servicesList.forEach(service => {
        const card = document.querySelector(`.service-card[data-service="${service.name}"]`);
        if (!card) return; // If card wasn't server-rendered (e.g. config updated since load), ignore or let it load on next SSR reload
        
        // 1. Update status pill
        const statusPill = card.querySelector(".status-pill");
        if (statusPill) {
            statusPill.className = `status-pill ${service.status === 'active' ? 'active' : (service.status === 'failed' ? 'failed' : 'inactive')}`;
            statusPill.textContent = service.status;
        }

        // 2. Update deployment history block
        const commitBlock = card.querySelector(".last-commit-block");
        if (commitBlock) {
            if (service.last_deployment) {
                const ld = service.last_deployment;
                const statusClass = ld.status === "success" ? "status-pill active" : (ld.status === "failed" ? "status-pill failed" : "status-pill deploying");
                
                const isRunning = ld.status === "running";
                const deployBtn = card.querySelector(".btn-primary");
                if (deployBtn) {
                    deployBtn.disabled = isRunning;
                }

                if (ld.commit_sha) {
                    commitBlock.innerHTML = `
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <span class="commit-sha">${ld.commit_sha.substring(0, 8)}</span>
                            <span class="${statusClass}">${ld.status}</span>
                        </div>
                        <p class="commit-msg">${ld.commit_message || 'Manual Trigger'}</p>
                        <p class="commit-author">by ${ld.author || 'system'} • ${formatDate(ld.started_at)}</p>
                    `;
                } else {
                    commitBlock.innerHTML = `
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <span style="font-weight: 500;">Manual Run</span>
                            <span class="${statusClass}">${ld.status}</span>
                        </div>
                        <p class="commit-author">Triggered at ${formatDate(ld.started_at)}</p>
                    `;
                }
            } else {
                commitBlock.innerHTML = `<p style="color: var(--text-dim); font-size: 0.825rem;">No deployments recorded yet.</p>`;
            }
        }
    });
}

// Render Deployment Audit Table
function renderHistory() {
    const tbody = document.getElementById("history-table-body");
    if (deploymentHistory.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="7" class="text-center">No deployments recorded yet.</td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = "";
    deploymentHistory.forEach(dep => {
        const tr = document.createElement("tr");
        const statusClass = dep.status === "success" ? "status-pill active" : (dep.status === "failed" ? "status-pill failed" : "status-pill deploying");
        
        let commitHTML = "-";
        if (dep.commit_sha) {
            commitHTML = `
                <div>
                    <span class="commit-sha">${dep.commit_sha.substring(0, 8)}</span>
                    <span class="commit-msg-inline" style="color: var(--text-muted); margin-left: 6px;">${dep.commit_message || ''}</span>
                </div>
            `;
        } else if (dep.trigger_type === "manual") {
            commitHTML = `<span style="color: var(--text-dim);">Manual trigger</span>`;
        }

        tr.innerHTML = `
            <td>#${dep.id}</td>
            <td><strong>${dep.repo_name}</strong></td>
            <td><span class="badge">${dep.trigger_type}</span></td>
            <td>${commitHTML}</td>
            <td>${formatDate(dep.started_at)}</td>
            <td><span class="${statusClass}">${dep.status}</span></td>
            <td class="text-right">
                <button class="btn btn-secondary btn-small" onclick="viewStaticLogs(${dep.id})">
                    View Logs
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

// Trigger Manual Deployment
async function triggerDeploy(repoName) {
    try {
        const response = await fetch(`/api/v1/services/${repoName}/deploy`, {
            method: "POST"
        });
        if (!response.ok) throw new Error("Failed to queue deployment");
        const data = await response.json();
        
        // Open live log stream
        connectLogStream(data.deployment_id, repoName);
    } catch (error) {
        alert(`Error triggering deploy: ${error.message}`);
    }
}

// Control Systemd Service (Restart/Stop/Start)
async function controlService(repoName, action) {
    if (!confirm(`Are you sure you want to ${action} the systemd service for ${repoName}?`)) return;
    try {
        const response = await fetch(`/api/v1/services/${repoName}/control`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ action: action })
        });
        if (!response.ok) throw new Error("Service command failed");
        alert(`Successfully dispatched ${action} command!`);
        pollServices();
    } catch (error) {
        alert(`Control Action Failed: ${error.message}`);
    }
}

// Fetch Live Console Logs (EventSource/SSE)
function connectLogStream(deployId, repoName) {
    if (logSource) {
        logSource.close();
    }

    consoleTitle.textContent = `Deployment Console Output: ${repoName} (#${deployId})`;
    terminalScreen.textContent = "";
    consoleModal.classList.remove("hidden");
    consoleStatusText.innerHTML = '<span class="spinner-small"></span> Deployment running...';

    logSource = new EventSource(`/api/v1/deployments/${deployId}/logs/stream`);

    logSource.onmessage = function(event) {
        if (event.data === "[DONE]") {
            logSource.close();
            consoleStatusText.textContent = "Deployment process finished.";
            fetchData();
            return;
        }
        terminalScreen.textContent += event.data + "\n";
        if (autoScroll) {
            terminalScreen.scrollTop = terminalScreen.scrollHeight;
        }
    };

    logSource.onerror = function(error) {
        logSource.close();
        consoleStatusText.textContent = "Logs stream complete or disconnected.";
        fetchData();
    };
}

// Close Console Modal
function closeConsole() {
    if (logSource) {
        logSource.close();
        logSource = null;
    }
    consoleModal.classList.add("hidden");
}

// View Static Logs (Completed Deployment History)
async function viewStaticLogs(deployId) {
    consoleTitle.textContent = `Deployment Logs: #${deployId}`;
    terminalScreen.textContent = "Retrieving archived logs...";
    consoleModal.classList.remove("hidden");
    consoleStatusText.textContent = "Archived execution logs.";
    
    try {
        const response = await fetch(`/api/v1/deployments/${deployId}/logs`);
        if (!response.ok) throw new Error("Unable to retrieve logs");
        const data = await response.json();
        terminalScreen.textContent = data.logs || "No output recorded for this run.";
        terminalScreen.scrollTop = terminalScreen.scrollHeight;
    } catch (error) {
        terminalScreen.textContent = `Failed to load logs: ${error.message}`;
    }
}

// Display systemd Journalctl Logs
async function showJournalLogs(repoName) {
    activeJournalService = repoName;
    journalTitle.textContent = `systemd Journalctl Logs: ${repoName}`;
    journalScreen.textContent = "Retrieving systemd journal entries...";
    journalModal.classList.remove("hidden");

    try {
        const response = await fetch(`/api/v1/services/${repoName}/journal`);
        if (!response.ok) throw new Error("Failed to load journal");
        const data = await response.json();
        journalScreen.textContent = data.logs || "No logs returned for this service.";
        journalScreen.scrollTop = journalScreen.scrollHeight;
    } catch (error) {
        journalScreen.textContent = `Failed to load journal logs: ${error.message}`;
    }
}

// Date Formatting Helper
function formatDate(isoStr) {
    if (!isoStr) return "-";
    const date = new Date(isoStr);
    return date.toLocaleString(undefined, { hour12: false });
}

async function loadConfig() {
    configSaveStatus.textContent = "";
    configSaveStatus.className = "config-save-status";
    configTextarea.value = "Loading...";
    updateGutter();
    try {
        const response = await fetch("/api/v1/config");
        if (!response.ok) throw new Error("Failed to fetch config");
        const data = await response.json();
        configTextarea.value = data.content;
        configOriginalContent = data.content;
        updateGutter();
        updateDirtyState();
        updateCursorPos();
    } catch (error) {
        configTextarea.value = "";
        configOriginalContent = null;
        setConfigStatus(`Error: ${error.message}`, "error");
    }
}

async function saveConfig() {
    setConfigStatus("Saving…", "");
    btnSaveConfig.disabled = true;
    try {
        const response = await fetch("/api/v1/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content: configTextarea.value }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Unknown error");
        configOriginalContent = configTextarea.value;
        updateDirtyState();
        setConfigStatus("✓ Saved", "success");
    } catch (error) {
        setConfigStatus(`✗ ${error.message}`, "error");
    } finally {
        btnSaveConfig.disabled = false;
        setTimeout(() => {
            configSaveStatus.textContent = "";
            configSaveStatus.className = "config-save-status";
        }, 4000);
    }
}

function setConfigStatus(text, cls) {
    configSaveStatus.textContent = text;
    configSaveStatus.className = "config-save-status" + (cls ? " " + cls : "");
}

function updateGutter() {
    const lines = configTextarea.value.split("\n").length;
    let html = "";
    for (let i = 1; i <= lines; i++) html += `<div>${i}</div>`;
    configGutterInner.innerHTML = html;
}

function updateDirtyState() {
    const dirty = configOriginalContent !== null && configTextarea.value !== configOriginalContent;
    configDirtyDot.classList.toggle("visible", dirty);
}

function updateCursorPos() {
    const pos = configTextarea.selectionStart;
    const lines = configTextarea.value.substring(0, pos).split("\n");
    const ln = lines.length;
    const col = lines[lines.length - 1].length + 1;
    configCursorPos.textContent = `Ln ${ln}, Col ${col}`;
}

