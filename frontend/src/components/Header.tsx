import React from 'react';

interface HeaderProps {
  onReset: () => void;
  isResetting: boolean;
}

export const Header: React.FC<HeaderProps> = ({ onReset, isResetting }) => {
  return (
    <header className="app-header">
      <div className="header-container">
        <div className="brand-section">
          <div className="logo-icon">DG</div>
          <div>
            <div style={{ display: 'flex', alignItems: 'center' }}>
              <h1 className="brand-title">DriftGuard</h1>
              <span className="brand-badge">PS01 Prototype</span>
            </div>
            <p className="brand-subtitle">Autonomous Tool Registry for AI Agents</p>
          </div>
        </div>

        <div className="header-actions">
          <div className="service-pill" title="Downstream Ticketing & User Mock Service">
            <span className="status-dot"></span>
            <span>Mock API: :8001</span>
          </div>

          <div className="service-pill" title="Autonomous Orchestrator & Tool Registry">
            <span className="status-dot"></span>
            <span>Orchestrator: :8000</span>
          </div>

          <button
            id="btn-reset-demo"
            className="btn btn-secondary"
            onClick={onReset}
            disabled={isResetting}
            title="Reset mock data fixtures and run event history"
          >
            {isResetting ? 'Resetting...' : 'Reset System'}
          </button>
        </div>
      </div>
    </header>
  );
};
