// Actual Astryx React islands. Native app.js owns network/state; these controls
// receive only view props and explicit callbacks, never credentials or API access.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { createPortal, flushSync } from 'react-dom';
import { Theme } from '@astryxdesign/core/theme';
import { neutralTheme } from '@astryxdesign/theme-neutral/built';
import { Button } from '@astryxdesign/core/Button';
import '@astryxdesign/core/reset.css';
import '@astryxdesign/core/astryx.css';
import '@astryxdesign/theme-neutral/theme.css';

let root;
let props;
function Controls() {
  return <Theme theme={neutralTheme} mode={props.theme}>
    {createPortal(<>
      <Button id="approve-button" className="button primary" label="Confirm rollback" size="lg" isDisabled={!props.canApprove} onClick={() => props.onConfirm(true)} />
      <Button id="cancel-button" className="button" label="Cancel" size="lg" isDisabled={!props.canCancel} onClick={() => props.onConfirm(false)} />
    </>, document.getElementById('approval-controls'))}
    {createPortal(<>
      <Button className="chip" label="Investigate" size="lg" isDisabled={props.commandsDisabled} onClick={() => props.onCommand("What's on fire?")} />
      <Button className="chip" label="Inspect logs" size="lg" isDisabled={props.commandsDisabled} onClick={() => props.onCommand('Dig into the logs')} />
      <Button className="chip" label="Review rollback" size="lg" isDisabled={props.commandsDisabled} onClick={() => props.onCommand('Roll back the latest deploy')} />
    </>, document.getElementById('suggested-controls'))}
  </Theme>;
}
export function updateControls(next) {
  props = next;
  if (!root) root = createRoot(document.getElementById('astryx-root'));
  flushSync(() => root.render(<Controls />));
}
