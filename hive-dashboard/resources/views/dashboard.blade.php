<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hive Mind Dashboard</title>
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; background: #f5f5f5; }
        .header { background: #1a1a1a; color: white; padding: 1rem 2rem; }
        .container { max-width: 1200px; margin: 2rem auto; padding: 0 2rem; }
        .fact-card { background: white; border-radius: 8px; padding: 1rem; margin: 1rem 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .tags { margin-top: 0.5rem; }
        .tag { background: #e3f2fd; color: #1565c0; padding: 0.25rem 0.5rem; border-radius: 4px; font-size: 0.8rem; margin-right: 0.5rem; }
        .meta { color: #666; font-size: 0.9rem; margin-top: 0.5rem; }
        .search { width: 100%; padding: 0.75rem; font-size: 1rem; border: 1px solid #ddd; border-radius: 4px; margin-bottom: 2rem; }
        .loading { text-align: center; padding: 2rem; color: #666; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
        .stat-card { background: white; padding: 1rem; border-radius: 8px; text-align: center; }
        .stat-number { font-size: 2rem; font-weight: bold; color: #1565c0; }
        .stat-label { color: #666; margin-top: 0.5rem; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🧠 Hive Mind Dashboard</h1>
        <p>Institutional Memory Across AI Agents</p>
    </div>

    <div class="container">
        <div class="stats" id="stats">
            <div class="stat-card">
                <div class="stat-number" id="fact-count">-</div>
                <div class="stat-label">Total Facts</div>
            </div>
            <div class="stat-card">
                <div class="stat-number" id="agent-count">-</div>
                <div class="stat-label">Active Agents</div>
            </div>
            <div class="stat-card">
                <div class="stat-number" id="sync-status">-</div>
                <div class="stat-label">Sync Status</div>
            </div>
        </div>

        <input type="text" class="search" placeholder="Search facts..." 
               hx-get="/api/sync/facts" 
               hx-trigger="keyup changed delay:300ms" 
               hx-target="#facts-container">

        <div id="facts-container" hx-get="/api/sync/facts" hx-trigger="load">
            <div class="loading">Loading facts...</div>
        </div>
    </div>

    <script>
        // Auto-refresh every 30 seconds
        setInterval(() => {
            htmx.trigger('#facts-container', 'load');
        }, 30000);

        // Handle HTMX responses
        document.addEventListener('htmx:afterRequest', function(evt) {
            if (evt.detail.target.id === 'facts-container') {
                try {
                    const response = JSON.parse(evt.detail.xhr.responseText);
                    if (response.success && response.facts) {
                        renderFacts(response.facts);
                        updateStats(response.facts);
                    }
                } catch (e) {
                    console.error('Failed to parse response:', e);
                }
            }
        });

        function renderFacts(facts) {
            const container = document.getElementById('facts-container');
            if (facts.length === 0) {
                container.innerHTML = '<div class="loading">No facts found</div>';
                return;
            }

            const html = facts.map(fact => `
                <div class="fact-card">
                    <div class="content">${escapeHtml(fact.content)}</div>
                    <div class="tags">
                        ${(JSON.parse(fact.tags || '[]')).map(tag => 
                            `<span class="tag">${escapeHtml(tag)}</span>`
                        ).join('')}
                    </div>
                    <div class="meta">
                        Source: ${escapeHtml(fact.source_agent)} | 
                        Trust: ${(fact.trust_score || 1).toFixed(2)} | 
                        Created: ${new Date(fact.created_at).toLocaleDateString()}
                    </div>
                </div>
            `).join('');
            
            container.innerHTML = html;
        }

        function updateStats(facts) {
            document.getElementById('fact-count').textContent = facts.length;
            
            const agents = [...new Set(facts.map(f => f.source_agent))];
            document.getElementById('agent-count').textContent = agents.length;
            
            document.getElementById('sync-status').textContent = '✓';
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
    </script>
</body>
</html>