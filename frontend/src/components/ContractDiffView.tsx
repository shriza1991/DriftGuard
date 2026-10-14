import React from 'react';

export const ContractDiffView: React.FC = () => {
  return (
    <div className="glass-card" style={{ height: '100%' }}>
      <div className="card-header">
        <div>
          <h2 className="card-title">Contract Diffs & Staged Patches</h2>
          <p className="card-subtitle">
            Autonomous schema adaptations proposed and hot-committed to the active registry
          </p>
        </div>
        <span className="badge-pending">Milestone 2 Scope</span>
      </div>

      <div className="empty-state">
        <div className="empty-state-icon">&Delta;</div>
        <div className="empty-state-title">No Active Contract Patches</div>
        <p className="empty-state-desc">
          Current runs are using pristine v1 contracts. When autonomous healing is integrated in Milestone 2,
          synthesized argument transforms and safety verification diffs will be rendered here.
        </p>

        <div
          style={{
            marginTop: '1.5rem',
            background: 'rgba(15, 23, 42, 0.8)',
            border: '1px solid var(--border-subtle)',
            borderRadius: 'var(--radius-md)',
            padding: '1rem',
            textAlign: 'left',
            fontFamily: 'var(--font-mono)',
            fontSize: '0.75rem',
            maxWidth: '460px',
            marginLeft: 'auto',
            marginRight: 'auto'
          }}
        >
          <div style={{ color: 'var(--text-muted)', marginBottom: '0.4rem' }}>// Milestone 2 Target Diff Preview:</div>
          <div style={{ color: '#ef4444' }}>- POST /tickets/assign &#123; user_id: &quot;123&quot; &#125;</div>
          <div style={{ color: '#10b981' }}>+ POST /tickets/assign &#123; assignee_id: &quot;123&quot; &#125;</div>
          <div style={{ color: 'var(--accent-cyan)', marginTop: '0.4rem' }}>
            Verification: GET /tickets/81 &rarr; verified_assignee: &quot;123&quot; (Safety Passed)
          </div>
        </div>
      </div>
    </div>
  );
};
