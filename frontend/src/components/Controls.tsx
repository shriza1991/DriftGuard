import React from 'react';

interface ControlsProps {
  mode: 'normal' | 'healing';
  setMode: (mode: 'normal' | 'healing') => void;
  task: string;
  setTask: (task: string) => void;
  onExecute: () => void;
  isRunning: boolean;
}

export const Controls: React.FC<ControlsProps> = ({
  mode,
  setMode,
  task,
  setTask,
  onExecute,
  isRunning,
}) => {
  return (
    <div className="glass-card">
      <div className="card-header">
        <div>
          <h2 className="card-title">
            <span>Agent Control & Workflow Invocations</span>
          </h2>
          <p className="card-subtitle">
            Execute canonical hackathon workflow: Find Maya's ID &rarr; Assign Ticket 81 &rarr; Verify Assignee
          </p>
        </div>
        <span className="badge-active">Deterministic v1 Fixtures Active</span>
      </div>

      <div className="controls-grid">
        <div className="form-group">
          <label className="form-label">Workflow Task Description</label>
          <input
            id="input-task"
            type="text"
            className="input-text"
            value={task}
            onChange={(e) => setTask(e.target.value)}
            disabled={isRunning}
          />
        </div>

        <div className="form-group">
          <label className="form-label">
            Execution Mode
            {mode === 'healing' && <span className="badge-pending">Pending Member 2</span>}
          </label>
          <select
            id="select-mode"
            className="select-input"
            value={mode}
            onChange={(e) => setMode(e.target.value as 'normal' | 'healing')}
            disabled={isRunning}
          >
            <option value="normal">Normal Baseline (Fixed v1 Registry)</option>
            <option value="healing">Autonomous Healing (Pending Milestone 2)</option>
          </select>
        </div>

        <div className="form-group">
          <label className="form-label">
            Drift Scenario
            <span className="badge-pending">Disabled (Pending Member 1)</span>
          </label>
          <select
            id="select-mutation"
            className="select-input"
            disabled={true}
            title="Mutation controls are disabled in Milestone 1 scaffolding. Pending Member 1 runtime mutation engine."
          >
            <option>None (Standard v1 Contracts)</option>
            <option disabled>1. Rename (user_id &rarr; assignee_id)</option>
            <option disabled>2. Type Change (string &rarr; integer)</option>
            <option disabled>3. Nesting (target.id)</option>
            <option disabled>4. Required Header (Authorization)</option>
            <option disabled>5. Combined Multi-Vector Drift</option>
            <option disabled>6. Missing Value (Refusal)</option>
            <option disabled>7. Unavailable Credential (Refusal)</option>
            <option disabled>8. Ambiguous Mapping (Refusal)</option>
          </select>
        </div>
      </div>

      <div style={{ marginTop: '1.25rem', display: 'flex', justifyContent: 'flex-end', gap: '0.75rem' }}>
        <button
          id="btn-execute-workflow"
          className="btn btn-primary"
          onClick={onExecute}
          disabled={isRunning}
        >
          {isRunning ? (
            <>
              <span className="status-dot" style={{ background: '#ffffff', boxShadow: 'none' }}></span>
              <span>Executing Workflow...</span>
            </>
          ) : (
            <span>Execute Workflow (Maya &rarr; Ticket 81)</span>
          )}
        </button>
      </div>
    </div>
  );
};
