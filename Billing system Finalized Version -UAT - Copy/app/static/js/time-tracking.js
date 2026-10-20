// app/static/js/time-tracking.js

let deleteEntryId = null;

function calculateEarnings() {
    const projectSelect = document.getElementById('project');
    const hours = parseFloat(document.getElementById('hours')?.value) || 0;
    const isBillable = document.getElementById('isBillable')?.checked;
    
    if (projectSelect?.value && hours > 0 && isBillable) {
        const selectedOption = projectSelect.options[projectSelect.selectedIndex];
        const rate = parseFloat(selectedOption.dataset.rate || 0);
        const earnings = hours * rate;
        
        const earningsElement = document.getElementById('earningsAmount');
        const earningsContainer = document.getElementById('estimatedEarnings');
        
        if (earningsElement && earningsContainer) {
            earningsElement.textContent = earnings.toFixed(2);
            earningsContainer.style.display = 'block';
        }
    } else {
        const earningsContainer = document.getElementById('estimatedEarnings');
        if (earningsContainer) {
            earningsContainer.style.display = 'none';
        }
    }
}

async function loadTimeEntries(filter = 'all') {
    const tbody = document.getElementById('entriesTableBody');
    if (!tbody) return;
    
    tbody.innerHTML = '<tr><td colspan="7" class="text-center"><div class="spinner-border spinner-border-sm" role="status"></div> Loading...</td></tr>';
    
    try {
        const response = await fetch('/api/time-entries');
        const entries = await response.json();
        
        if (entries.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">No time entries yet. Log your first time entry!</td></tr>';
            return;
        }
        
        // Filter entries
        let filteredEntries = entries;
        const today = new Date();
        const weekAgo = new Date(today);
        weekAgo.setDate(weekAgo.getDate() - 7);
        const monthStart = new Date(today.getFullYear(), today.getMonth(), 1);
        
        if (filter === 'week') {
            filteredEntries = entries.filter(e => new Date(e.date) >= weekAgo);
        } else if (filter === 'month') {
            filteredEntries = entries.filter(e => new Date(e.date) >= monthStart);
        }
        
        // Sort by date descending
        filteredEntries.sort((a, b) => new Date(b.date) - new Date(a.date));
        
        // Build table HTML
        let html = '';
        filteredEntries.forEach(entry => {
            html += `
                <tr>
                    <td>${entry.date}</td>
                    <td>${entry.project}</td>
                    <td>${entry.description.substring(0, 30)}${entry.description.length > 30 ? '...' : ''}</td>
                    <td>${entry.hours.toFixed(2)}</td>
                    <td>$${entry.earnings.toFixed(2)}</td>
                    <td>
                        ${entry.is_billable ? 
                            '<span class="badge bg-success">Billable</span>' : 
                            '<span class="badge bg-secondary">Non-billable</span>'}
                    </td>
                    <td>
                        <button class="btn btn-sm btn-outline-danger" onclick="showDeleteModal(${entry.id})">
                            <i class="bi bi-trash"></i>
                        </button>
                    </td>
                </tr>
            `;
        });
        
        tbody.innerHTML = html;
        
    } catch (error) {
        console.error('Error:', error);
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-danger">Error loading entries</td></tr>';
    }
}

async function updateSummary() {
    try {
        const response = await fetch('/api/summary');
        const summary = await response.json();
        
        const elements = {
            todayHours: document.getElementById('todayHours'),
            weekHours: document.getElementById('weekHours'),
            billableHours: document.getElementById('billableHours'),
            estimatedRevenue: document.getElementById('estimatedRevenue')
        };
        
        if (elements.todayHours) elements.todayHours.textContent = summary.today.toFixed(1);
        if (elements.weekHours) elements.weekHours.textContent = summary.week.toFixed(1);
        if (elements.billableHours) elements.billableHours.textContent = summary.billable.toFixed(1);
        if (elements.estimatedRevenue) {
            elements.estimatedRevenue.textContent = '$' + summary.revenue.toFixed(2);
        }
        
    } catch (error) {
        console.error('Error updating summary:', error);
    }
}

function showDeleteModal(entryId) {
    deleteEntryId = entryId;
    const modal = new bootstrap.Modal(document.getElementById('deleteModal'));
    modal.show();
}

async function deleteTimeEntry() {
    if (!deleteEntryId) return;
    
    try {
        const response = await fetch(`/api/time-entries/${deleteEntryId}`, {
            method: 'DELETE'
        });
        
        const result = await response.json();
        
        if (result.success) {
            bootstrap.Modal.getInstance(document.getElementById('deleteModal')).hide();
            loadTimeEntries();
            updateSummary();
            showToast('Time entry deleted successfully', 'success');
        } else {
            showToast('Error: ' + (result.error || 'Failed to delete entry'), 'danger');
        }
    } catch (error) {
        console.error('Error:', error);
        showToast('Error deleting entry', 'danger');
    }
}

function showToast(message, type = 'info') {
    // Get or create toast container
    let toastContainer = document.querySelector('.toast-container');
    if (!toastContainer) {
        toastContainer = document.createElement('div');
        toastContainer.className = 'toast-container position-fixed bottom-0 end-0 p-3';
        document.body.appendChild(toastContainer);
    }
    
    // Create toast element
    const toastId = 'toast-' + Date.now();
    const toast = document.createElement('div');
    toast.className = `toast align-items-center text-white bg-${type} border-0`;
    toast.id = toastId;
    toast.setAttribute('role', 'alert');
    toast.setAttribute('aria-live', 'assertive');
    toast.setAttribute('aria-atomic', 'true');
    
    toast.innerHTML = `
        <div class="d-flex">
            <div class="toast-body">
                ${message}
            </div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
        </div>
    `;
    
    toastContainer.appendChild(toast);
    
    // Initialize and show toast
    const bsToast = new bootstrap.Toast(toast, { delay: 3000 });
    bsToast.show();
    
    // Remove toast after hidden
    toast.addEventListener('hidden.bs.toast', () => {
        toast.remove();
    });
}

function filterEntries(period) {
    loadTimeEntries(period);
}

// Expose functions to global scope
window.loadTimeEntries = loadTimeEntries;
window.showDeleteModal = showDeleteModal;
window.deleteTimeEntry = deleteTimeEntry;
window.filterEntries = filterEntries;
window.calculateEarnings = calculateEarnings;
window.updateSummary = updateSummary;
window.showToast = showToast;