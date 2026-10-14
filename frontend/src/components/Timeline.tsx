import React from 'react';
import { RunEvent, RunSummary } from '../types';

interface TimelineProps {
  events: RunEvent[];
  currentRun: RunSummary | null;
}

export const Timeline: React.FC<TimelineProps> = ({ events, currentRun }) => {
  return (
    <div className="glass-card" style={{ height: '100%' }}>
      <div className="card-header">
        <div>
          <h2 className="card-title">Live Event Stream</h2>
          <p className="card-subtitle">
            Server-Sent Events (SSE) emitted during workflow lifecycle across <code>/runs/:id/events</code>
          </p>
        </div>
        {currentRun && (
          <span className={`badge-${currentRun.status === 'completed' ? 'active' : 'pending'}`}>
            Run {currentRun.run_id} ({currentRun.status})
          </span>
        )}
      </div>

      {events.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state-icon">&harr;</div>
          <div className="empty-state-title">No Active Workflow Stream</div>
          <p className="empty-state-desc">
            Click &quot;Execute Workflow&quot; above to trigger the Maya &rarr; Ticket 81 assignment workflow and stream real-time events.
          </p>
        </div>
      ) : (
        <div className="timeline-list">
          {events.map((ev) => (
            <div key={`${ev.run_id}-${ev.sequence}`} className="timeline-item">
              <div className="timeline-seq">#{ev.sequence}</div>
              <div className="timeline-content">
                <div className="timeline-meta">
                  <span className={`event-type-badge event-${ev.type}`}>
                    {ev.type}
                  </span>
                  <span className="timeline-time">
                    {new Date(ev.timestamp).toLocaleTimeString()}
                  </span>
                </div>

                {ev.tool && (
                  <div className="timeline-tool">
                    Tool: <code>{ev.tool}</code>
                  </div>
                )}

                {Object.keys(ev.data).length > 0 && (
                  <pre className="timeline-payload">
                    {JSON.stringify(ev.data, null, 2)}
                  </pre>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
