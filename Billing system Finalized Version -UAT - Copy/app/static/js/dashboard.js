// app/static/js/dashboard.js

document.addEventListener('DOMContentLoaded', function() {
    console.log('Dashboard loaded');
    
    // Add animation to stats cards
    const cards = document.querySelectorAll('.stats-card');
    cards.forEach((card, index) => {
        card.style.animation = `fadeIn 0.5s ease-out ${index * 0.1}s both`;
    });
    
    // Refresh data every 30 seconds
    setInterval(refreshDashboardData, 30000);
});

async function refreshDashboardData() {
    try {
        const response = await fetch('/api/summary');
        const data = await response.json();
        
        // Update summary numbers if they exist on dashboard
        const weekHoursElement = document.querySelector('.stats-card .h5');
        if (weekHoursElement) {
            // You can update specific elements here if needed
            console.log('Dashboard data refreshed:', data);
        }
    } catch (error) {
        console.error('Error refreshing dashboard:', error);
    }
}

// Add tooltips
document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(element => {
    new bootstrap.Tooltip(element);
});