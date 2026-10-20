/* ========================================
   MODALS.JS - Modal Management
   ======================================== */

// This file is now deprecated - use InvoiceSystem from utils.js
// Kept for backward compatibility
window.viewServiceDetails = function(serviceId) {
    InvoiceSystem.viewServiceDetails(serviceId);
};

window.viewWorkflow = function(serviceId) {
    InvoiceSystem.viewWorkflow(serviceId);
};

window.viewWorkflowFromModal = function() {
    InvoiceSystem.viewWorkflowFromModal();
};

window.showDocumentModal = function(companyId, serviceItemId) {
    InvoiceSystem.showDocumentModal(companyId, serviceItemId);
};

window.formatCurrency = function(amount) {
    return InvoiceSystem.formatCurrency(amount);
};

window.escapeHtml = function(text) {
    return InvoiceSystem.escapeHtml(text);
};

window.convertUTCToLocal = function() {
    InvoiceSystem.convertUTCToLocal();
};