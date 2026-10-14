import React from 'react';
import { ToolRegistryEntry } from '../types';

interface RegistryViewProps {
  tools: ToolRegistryEntry[];
}

export const RegistryView: React.FC<RegistryViewProps> = ({ tools }) => {
  return (
    <div className="glass-card">
      <div className="card-header">
        <div>
          <h2 className="card-title">Active Tool Registry</h2>
          <p className="card-subtitle">
            Frozen v1 tool contracts with cryptographic fingerprints and parameter schemas
          </p>
        </div>
        <span className="badge-active">{tools.length} Tools Active</span>
      </div>

      <div className="tools-grid">
        {tools.map((tool) => (
          <div key={tool.name} className="tool-card">
            <div className="tool-header">
              <span className="tool-name">{tool.name}</span>
              <span className={`tool-method-${tool.method.toLowerCase()}`}>
                {tool.method}
              </span>
            </div>

            <div className="tool-endpoint">{tool.endpoint}</div>
            <p className="tool-desc">{tool.description}</p>

            <div style={{ marginTop: 'auto', paddingTop: '0.5rem' }}>
              <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginBottom: '0.2rem' }}>
                Required Arguments:
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.35rem', marginBottom: '0.5rem' }}>
                {Object.keys(tool.argument_schema.properties).map((prop) => (
                  <span
                    key={prop}
                    style={{
                      background: 'rgba(255, 255, 255, 0.05)',
                      padding: '0.15rem 0.4rem',
                      borderRadius: '4px',
                      fontSize: '0.72rem',
                      fontFamily: 'var(--font-mono)'
                    }}
                  >
                    {prop}: {tool.argument_schema.properties[prop].type}
                  </span>
                ))}
              </div>

              <div className="tool-fingerprint">
                FP: <code>{tool.fingerprint || 'fp_v1_active'}</code> &bull; v{tool.version}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
