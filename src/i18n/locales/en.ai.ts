/* ==========================================================================
   pvecenter — English messages · ai（业务域）
   --------------------------------------------------------------------------
   与中文同组词条一一对应，按同样的路由时机加载（见 i18n/index.tsx）。
   声明成 Partial：没翻的词条直接不写，运行时会回退中文原文。
   ========================================================================== */

import type { MessageKey } from './zh-CN';

export const enAi: Partial<Record<MessageKey, string>> = {
  'ai.title': 'AI assistant',
  'ai.subtitle':
    'Describe the symptom or just ask — the AI signs in to the host and can also investigate VMs you have granted. Read-only commands run directly; anything that changes the system or installs software needs your approval, step by step.',
  'ai.runningHint': 'Calling the model and running commands; usually 10-60 seconds',
  'ai.settings': 'Model settings',
  'ai.selectTarget': 'Target machine',
  'ai.selectTargetPlaceholder': 'Select a machine…',
  'ai.selectTargetHint':
    'Pick one machine — host commands and in-VM access both follow it, no second choice',
  'ai.targetBoth': 'host + in-VM',
  'ai.targetHostOnly': 'host commands only',
  'ai.targetHostOnlyHint':
    'This machine is not registered as a VM (or is not running), so only host commands are available — in-VM access is not.',
  'ai.pickHostTitle': 'Pick the host to inspect first',
  'ai.pickHostHint':
    'The AI signs in to that host to read logs and run commands, so the target must be chosen by you — the panel never preselects one, because inspecting the wrong machine is far worse than one extra click.',
  'ai.noHostTitle': 'No managed host yet',
  'ai.noHostHint':
    'Inspection only works on servers registered under “SSH security → Managed hosts”. Add the machine you want to inspect first; then it shows up here.',
  'ai.notConfigured': 'No model configured yet',
  'ai.notConfiguredHint':
    'The assistant needs an LLM. External APIs (DeepSeek / OpenAI / …) and self-hosted models (vLLM / Ollama) are configured the same way.',
  'ai.configureNow': 'Configure',

  'ai.settingsTitle': 'Model settings',
  'ai.settingsDesc': 'Applies to the whole platform; the key is encrypted on the backend and never sent to the browser',
  'ai.enable': 'Enable the AI assistant',
  'ai.enableHint': 'When off, nobody can start an inspection',
  'ai.provider': 'Model',
  'ai.providerName': 'Name',
  'ai.providerNameHint': 'To tell several models apart, e.g. “DeepSeek (external)”',
  'ai.providerKind': 'Type',
  'ai.kindExternal': 'External API (public internet)',
  'ai.kindInternal': 'Self-hosted (no egress)',
  'ai.baseUrl': 'Base URL',
  'ai.baseUrlHint': 'e.g. https://api.example.com/v1 (an OpenAI-compatible endpoint)',
  'ai.apiKey': 'API key',
  'ai.apiKeyHint': 'Leave blank to keep the saved key',
  'ai.apiKeyPlaceholder': 'sk-… (may be blank for a self-hosted model)',
  'ai.apiKeyKeep': 'A key is saved; leave blank to keep it',
  'ai.model': 'Model name',
  'ai.modelHint': 'e.g. deepseek-chat, qwen2.5:14b',
  'ai.addProvider': 'Add model',
  'ai.removeProvider': 'Remove',
  'ai.test': 'Test connection',
  'ai.testOk': 'Connected',
  'ai.testFailed': 'Connection failed',
  'ai.save': 'Save settings',
  'ai.saved': 'Settings saved',
  'ai.saveFailed': 'Failed to save',
  'ai.fieldRequired': 'Name, base URL and model name are required',
  'ai.hours': 'Analysis window (hours)',
  'ai.hoursHint': 'How far back the collected data reaches; 24 hours by default',
  'ai.tokenBudget': 'Token budget per run',
  'ai.tokenBudgetHint':
    'Accumulated across rounds; once reached the model concludes from what it has. Default 60000, up to 100 million. The real brakes are the round count and the overall timeout — this is just a backstop.',
  'ai.replyCharsTerse': 'Reply length — chat (chars)',
  'ai.replyCharsTerseHint':
    'Character limit for everyday Q&A answers; 150 by default. Too short drops the evidence, too long reads like a dump of the tool output.',
  'ai.replyCharsDeep': 'Reply length — playbook (chars)',
  'ai.replyCharsDeepHint':
    'Character limit for the report produced after a playbook run; 400 by default.',

  'ai.failed': 'Inspection failed',
  'ai.disclaimer':
    'The AI output is advisory and may miss or misjudge things. It signs in to the target host and runs commands (disk, processes, logs, config): read-only diagnostics run directly, while anything that changes the system needs your approval command by command. Every command it runs is listed under “Progress” on the left — check it before acting.',
  'ai.readonlyTip':
    'Inspections default to built-in read-only commands (df / ps / ss / journalctl …). Every tool and argument is validated against an allowlist, and sudo is never used; once on-host execution is granted, the AI may also propose commands that change the system, but each one needs your approval. Use a regular account on managed hosts — do not give the assistant root.',

  'ai.progressTitle': 'Progress',
  'ai.progressDone': 'Finished',
  'ai.stageCollect': 'Collecting inspection data…',
  'ai.stepN': 'Analysis round {n} / {max}',
  'ai.toolStart': 'Calling {tool}{args}',
  'ai.toolDone': '{mark} {tool} finished ({ms} ms)',
  'ai.progressIdle': 'Not started',
  'ai.progressEmpty':
    'Once you run a health check or ask a question, every step the AI takes shows up here live.',
  'ai.chatTitle': 'Chat & approval',
  'ai.chatSubtitle': 'Approve the AI’s requests one by one',
  'ai.chatIntro':
    'Describe the symptom or just ask a question (e.g. “why is nginx returning 502?”) and I’ll dig in as we go. Want a full report instead? Click “Run health check”.',
  'ai.chatUserRequest': 'Health check on {host} (model: {model})',
  'ai.chatWorking': 'Looking things up and analysing…',
  'ai.chatPlaceholder': 'Ask a follow-up…',
  'ai.chatSend': 'Send',
  'ai.chatTarget': 'Target host: {host}',
  'ai.playbookPick': 'Run a playbook…',
  'ai.playbookPickHint':
    'Pick a preset troubleshooting playbook and work through it in order',
  'ai.needsHostTitle': 'Pick the host to inspect first',
  'ai.needsHostHint':
    'This question came from another page, but that machine is not registered as a '
    + 'managed host, so the AI cannot reach it. To avoid inspecting the wrong machine '
    + 'we will not pick one for you — choose a target host above (or add the machine '
    + 'under “SSH security → Managed hosts”).',
  'ai.chatNew': 'New chat',
  'ai.chatNewHint': 'Start a fresh conversation (does not reuse the current context)',
  'ai.chatHistory': 'History',
  'ai.progressHistory': 'Actions taken in this conversation',
  'ai.toolHistoryLine': '{mark} {tool}',

  /* 一键体检与报告卡 */
  'ai.start': 'Run health check',
  'ai.startHint': 'Collects the check data and produces a full report. To just ask something, type in the box below.',
  'ai.running': 'Running',
  'ai.quickInspect': 'Digest only',
  'ai.quickHint':
    'Reads the existing inspection results only — no host login, no tool calls. Use this with models that do not support tool calling.',
  'ai.summaryTitle': 'Summary',
  'ai.findingsTitle': 'Findings',
  'ai.findingsHint': 'Sorted by severity; click a title for evidence and the suggested action',
  'ai.noFindings': 'Nothing needs attention.',
  'ai.evidence': 'Evidence',
  'ai.suggestion': 'Suggested action',
  'ai.dataGaps': 'Still unclear',
  'ai.dataGapsHint': 'Without these the conclusion is not final',
  'ai.confidence': 'Confidence',
  'ai.metaModel': 'Model: {model}',
  'ai.metaTime': '{ms} ms',
  'ai.metaTokens': 'Tokens: {tokens}',
  'ai.metaTools': '{n} tool call(s)',
  'ai.viewDetails': 'Show evidence',
  'ai.hideDetails': 'Hide evidence',
  'ai.remediation.action': 'Fix it',
  'ai.reinspect': 'Re-inspect',
  'ai.diffTitle': 'Compared with the previous run',
  'ai.diffAdded': '{n} new',
  'ai.diffResolved': '{n} resolved',
  'ai.diffSame': 'The rest is unchanged',
  'ai.gotoLabel': 'Go to',
  'ai.recConclusion': 'Conclusion',
  'ai.recConvReport': 'Report',
  'ai.recConvHasReport': 'Yes',
  'ai.recConvNoReport': 'No',
  'ai.doneTitle': 'Health check finished',
  'ai.doneDetail': '{n} item(s) need attention',

  /* Remote terminal (left pane) */
  'ai.terminalTitle': 'Server terminal',
  'ai.terminalSubtitle': 'Work directly on the managed host',
  'ai.terminalHint':
    'Terminal: {host}. Commands you type and commands the AI runs both appear in this window.',
  'ai.terminalIdle': 'Not connected',
  'ai.terminalConnecting': 'Connecting…',
  'ai.terminalConnected': 'Connected',
  'ai.terminalDisconnected': 'Disconnected',
  'ai.terminalClosed': 'Connection closed',
  'ai.terminalError': 'Terminal connection failed',
  'ai.terminalErrorHint':
    'Even the preflight check did not complete — usually your session has expired or the panel is unreachable. Sign in again and retry; if it still fails, look for the “AI 终端握手被拒” line in the backend log.',
  'ai.terminalStaleHint':
    'The backend has no “terminal preflight” endpoint, which means it is still running an older build. Restart the backend (and rebuild the frontend), then retry.',
  'ai.terminalProxyHint':
    'The backend preflight passed (permission, host ownership and SSH reachability are all fine) but the WebSocket handshake did not succeed — usually the reverse proxy is not forwarding WebSocket. Make sure the nginx location block sets proxy_set_header Upgrade $http_upgrade; and proxy_set_header Connection "upgrade"; (see the “Enabling HTTPS” section of the README for a full example).',
  'ai.terminalReconnect': 'Reconnect',
  'ai.terminalReconnectHint': 'Drop the current connection and open a new terminal',
  'ai.terminalNoPermission':
    'You do not have the “Remote terminal” permission, so you cannot sign in to a managed host from here. Ask an administrator to grant it in your role.',
  'ai.terminalNeedHost':
    'Pick a managed host above first — the terminal only opens for managed hosts (the panel host itself is not exposed).',

  /* Token usage monitoring */
  'ai.usage.open': 'Token usage',
  'ai.usage.title': 'Token usage',
  'ai.usage.heroCaption': 'Last {days} days',
  'ai.usage.modalDesc':
    'Tokens spent, runs and per-model breakdown for the selected window.',
  'ai.usage.subtitle': 'Last {days} days · {runs} runs',
  'ai.usage.subtitleEmpty': 'No usage in this window',
  'ai.usage.windowDays': '{n}d',
  'ai.usage.windowLabel': 'Window',
  'ai.usage.totalTokens': 'Total',
  'ai.usage.promptTokens': 'Input',
  'ai.usage.completionTokens': 'Output',
  'ai.usage.runs': 'Runs',
  'ai.usage.avgTokens': 'Avg / run',
  'ai.usage.toolCalls': 'Tool calls',
  'ai.usage.avgDuration': 'Avg duration',
  'ai.usage.splitAria': 'Input vs output token split',
  'ai.usage.unit': 'tokens',
  'ai.usage.trend': 'Daily usage',
  'ai.usage.dayTip': '{day} · {n} tokens',
  'ai.usage.byModel': 'By model',
  'ai.usage.runsUnit': '{n} runs',
  'ai.usage.unknownModel': 'Unknown model',
  'ai.usage.noData': 'No usage data',
  'ai.usage.noDataDesc':
    'Run an inspection or ask a question, and the tokens spent over the last {n} days will show up here.',
  'ai.usage.loadFailed': 'Cannot load usage',
  'ai.usage.loadFailedMsg':
    'Try again later, or check each run in “Inspection history”.',

  'ai.tabInspect': 'Inspect',
  'ai.tabRecords': 'History',

  'ai.preset': 'Vendor preset',
  'ai.presetHint':
    'Pick one to fill in the endpoint and common model names automatically, or choose manual entry',
  'ai.presetManual': 'Manual entry',
  'ai.presetCustom': 'Other OpenAI-compatible service',
  'ai.modelSuggest': 'Common models:',
  'ai.recTime': 'Time',
  'ai.recHost': 'Host',
  'ai.recModel': 'Model',
  'ai.recStatus': 'Status',
  'ai.recSteps': 'Rounds',
  'ai.recTools': 'Tool calls',
  'ai.recTokens': 'Tokens',
  'ai.recDuration': 'Duration',
  'ai.recFilterHost': 'Filter by host',
  'ai.recAllHosts': 'All hosts',
  'ai.recTotal': '{n} record(s)',
  'ai.recEmptyTitle': 'No inspection history yet',
  'ai.recEmptyDesc': 'Once you run an inspection, every step the AI took shows up here.',
  'ai.recClickHint': 'Click to see the tools this run executed',
  'ai.recPrev': 'Previous',
  'ai.recNext': 'Next',
  'ai.recDetail': 'Tool call details',
  'ai.recDetailHint': 'What the AI actually executed during this run',
  'ai.recClose': 'Collapse',
  'ai.recOpenConversation': 'Open in chat',
  'ai.recOpenConversationHint':
    'Open this run’s conversation on the Inspect tab and keep asking follow-ups',
  'ai.recNoCalls': 'This run called no tools (digest only)',
  'ai.recViewRuns': 'Runs',
  'ai.recViewConversations': 'Conversations',
  'ai.recConvRuns': 'Runs',
  'ai.recConvEmptyTitle': 'No conversations yet',
  'ai.recConvEmptyDesc':
    'Start an inspection on the Inspect tab and a conversation will be created.',
  'ai.recConvDetail': 'Conversation',
  'ai.recConvDetailHint': 'Messages and tool calls in this conversation',
  'ai.recConvEmptyMessages': 'This conversation has no messages yet',
  'ai.recRoleUser': 'User',
  'ai.recRoleAssistant': 'AI',
  'ai.recRoleTool': 'Tool',
  'ai.sharedModel': 'Shared',
  'ai.capabilities': 'Capabilities',
  'ai.sectionTarget': 'Target and model',
  'ai.sectionTargetHint': 'Pick the machine and which model handles this investigation',
  'ai.sectionScope': 'Access scope',
  'ai.sectionScopeHint':
    'Two channels granted separately: the target host (this session) and VMs (time-limited tickets)',
  'ai.execScopeName': 'Host commands',
  'ai.execScopeOn': 'This session · allowed',
  'ai.execScopeOff': 'This session · not allowed',
  'ai.execScopeToggle': 'Allow the AI to run commands on the host',
  'ai.continue': 'Continue',
  'ai.continueHint':
    'Execution is granted. The switch applies to the next turn, so use this to pick up where the last one stopped.',
  'ai.continuePrompt': 'Please continue from the previous step — host execution is now granted.',
  'ai.execNotGranted': 'Host execution not granted',
  'ai.sectionConsole': 'Execution and conversation',
  'ai.sectionConsoleHint':
    'Live terminal and progress on the left, conversation and per-step approvals on the right',
  'ai.allowExec': 'Let the AI run commands on the target host',
  'ai.allowExecHint':
    'Read-only diagnostics run directly (e.g. ps aux | grep nginx); commands that change the system or restart services ask for approval first and only run once you approve them, and everything is recorded.',
  'ai.allowExecWarn':
    'Commands that change the system run only after you approve them one by one; the grant applies to this run only.',
  'ai.approval.title': 'Approve the AI’s command',
  'ai.approval.badge': 'Write',
  'ai.approval.countdown': 'auto-rejected in {s}s',
  'ai.approval.purpose': 'Why the AI wants it',
  'ai.approval.command': 'Command to run',
  'ai.approval.approve': 'Approve and run',
  'ai.approval.reject': 'Reject',
  'ai.approval.stale':
    'This request is no longer pending (it timed out or was already handled). Ask the AI to propose it again.',
  'ai.approval.requiredTwo': 'Needs {n} approvals',
  'ai.approval.voted': 'Approved 1/{n}',
  'ai.approval.waitSecond':
    'Your vote is recorded; waiting for another permitted user. If the count is not reached in time, it is treated as a rejection.',
  'ai.approval.vmBadge': 'In-guest change',
  'ai.approval.target': 'Target and action',
  'ai.approval.diff': 'Preview of changes',
  /* In-guest investigation grants */
  'ai.lease.title': 'In-guest investigation',
  'ai.lease.noneBadge': 'Not granted',
  'ai.lease.noneHint':
    'Once granted, the AI can read logs, change configs and install packages inside the VM — pick an access level; every change still needs your approval',
  'ai.lease.open': 'Grant',
  'ai.lease.collapse': 'Collapse',
  'ai.lease.target': 'Target VM',
  'ai.lease.agentChecking': 'Checking the Guest Agent…',
  'ai.lease.agentReady': 'Running · managed',
  'ai.lease.agentReadyHint': 'The Guest Agent answered; the AI can enter once granted',
  'ai.lease.agentMissing': 'Guest Agent not responding',
  'ai.lease.agentMissingHint':
    'This VM may be stopped, or qemu-guest-agent is not installed/running, or agent is disabled in its config. Unmanaged VMs cannot be granted.',
  'ai.lease.minutes': 'Duration',
  'ai.lease.minutesHint': 'Expires automatically; can be extended once',
  'ai.lease.min15': '15 minutes',
  'ai.lease.min30': '30 minutes',
  'ai.lease.min60': '1 hour',
  'ai.lease.min120': '2 hours',
  'ai.lease.writable': 'Writable directories',
  'ai.lease.writableHint':
    'Leave empty for a read-only grant; every write is approved separately',
  'ai.lease.units': 'Restartable services',
  'ai.lease.unitsHint':
    'systemd unit names, comma separated; empty means no service may be restarted',
  'ai.lease.reason': 'Reason',
  'ai.lease.reasonHint': 'Written to the audit log for later review',
  'ai.lease.reasonPlaceholder': 'e.g. investigating intermittent 502s on the order service',
  'ai.lease.grant': 'Grant access',
  'ai.lease.writeWarn':
    'Includes writes: each one shows a diff for approval and backs up the original file plus a snapshot',
  'ai.lease.readOnlyHint':
    'Read-only grant: the AI can list directories, read files and run read-only diagnostics',
  'ai.lease.pickRequired': 'Pick a VM to grant access to',
  'ai.lease.active': 'Active',
  'ai.lease.expired': 'Expired',
  'ai.lease.left': '{time} left',
  'ai.lease.quota': 'actions {used}/{max}',
  'ai.lease.fsRo': 'Readable',
  'ai.lease.fsRw': 'Writable',
  'ai.lease.unitsLabel': 'Restartable',
  'ai.lease.readOnly': 'read-only',
  'ai.lease.none': 'none',
  'ai.lease.snapshot': 'Rollback snapshot',
  'ai.lease.snapshotNone': 'None (storage may not support snapshots; file-level backups are used)',
  'ai.lease.noActions': 'No actions yet. Every step the AI takes will be listed here.',
  'ai.lease.denied': 'Denied',
  'ai.lease.rollbackFile': 'Roll back this file',
  'ai.lease.rollbackSnapshot': 'Roll back to snapshot',
  'ai.lease.revoke': 'Revoke access',
  'ai.lease.revoked': 'Access revoked',
  'ai.lease.revokedByUser': 'Revoked by the user',
  'ai.lease.revokedKillFailed':
    'Access revoked, but the running command in the guest could not be stopped',
  'ai.lease.inChatHint': 'Ask the AI below to start investigating',
  'ai.lease.osLinux': 'Linux',
  'ai.lease.osWindows': 'Windows',
  'ai.lease.modeSandbox': 'Sandbox mode',
  'ai.lease.modeCompat': 'Compat mode',
  'ai.lease.modeSandboxHint':
    'diagd runs inside the guest: commands have CPU/memory caps and can be killed',
  'ai.lease.modeCompatHint':
    'diagd is not installed: isolation relies only on the server-side allowlist, and running commands cannot be killed. Install diagd and re-grant to upgrade to sandbox mode.',
  'ai.lease.tags': 'Tags',
  'ai.lease.noTags': 'no tags',
  'ai.lease.policyBlocked': 'Blocked by policy',
  'ai.lease.doubleApprove': 'Two-person approval',
  'ai.lease.doubleApproveHint':
    'This grant was issued with two-person approval: config changes, service restarts and similar high-risk actions need two different users with approval rights, so one person cannot release them alone. If no second user holds approval rights, it falls back to single-person approval automatically. Administrators can turn two-person approval off in “VM grant policy” to use single-person approval instead.',
  'ai.lease.kill': 'Kill command',
  'ai.lease.killHint': 'Stops the running command only; the grant stays valid',
  'ai.lease.killOk': 'Command killed',
  'ai.lease.killFailed': 'Kill failed',
  'ai.lease.diagd': 'Get the diagd installer',
  'ai.lease.diagdCollapse': 'Hide the installer',
  'ai.lease.diagdVersion': 'diagd {version}: run this script as root inside the guest',
  'ai.lease.diagdCopy': 'Copy script',
  'ai.lease.diagdCopied': 'Installer copied',
  'ai.lease.diagdLoadFailed': 'Failed to load the installer',
  'ai.lease.access': 'Access level',
  'ai.lease.accessHint':
    'How far the AI may go. The level is fixed when the grant is issued and never widens by itself',
  'ai.lease.accessRead': 'Read-only investigation',
  'ai.lease.accessWrite': 'Scoped changes (selected paths / services)',
  'ai.lease.accessOps': 'Ops access (can install packages)',
  'ai.lease.accessReadShort': 'Read-only',
  'ai.lease.accessWriteShort': 'Scoped',
  'ai.lease.accessOpsShort': 'Ops access',
  'ai.lease.accessOpsWarn':
    'Ops access lets the AI run any command as root: install packages, edit any config, '
    + 'restart any service. Every mutating step still shows an approval card; irreversible '
    + 'actions (removing the root filesystem, repartitioning, rebooting the VM, altering '
    + 'credentials) are blocked server-side. Turning on automatic snapshots is recommended '
    + 'so the whole VM can be rolled back.',
  'ai.lease.accessOpsShortWarn':
    'Ops access: the AI can install packages and edit configs, and will ask for approval first',
  'ai.lease.accessOpsActive':
    'Ops access is active: the AI can install packages, edit any config and restart services. '
    + 'Every change still asks for approval; removing the root filesystem, repartitioning, '
    + 'rebooting the VM and altering credentials are refused server-side.',
  'ai.lease.activeList': 'Active grants (still valid after a page reload)',
  'ai.lease.activeListHint': 'Continue with or revoke one here',
  'ai.lease.activeCount': '{n} active',
  'ai.lease.useThis': 'Use this',
  'ai.lease.revokeThis': 'Revoke this',
  'ai.vmPolicy.title': 'VM access policy',
  'ai.vmPolicy.hint':
    'How far the AI may go: which VMs are write-protected, whether high-risk actions need two approvals (optional — single approval is supported too), and how long snapshots are kept',
  'ai.vmPolicy.loading': 'Loading policy…',
  'ai.vmPolicy.save': 'Save policy',
  'ai.vmPolicy.saved': 'Policy saved',
  'ai.vmPolicy.saveFailed': 'Failed to save policy',
  'ai.vmPolicy.maxMinutes': 'Max grant duration (minutes)',
  'ai.vmPolicy.maxMinutesHint': 'Longer requests are clamped to this value; range 5–480',
  'ai.vmPolicy.keepHours': 'Snapshot retention (hours)',
  'ai.vmPolicy.keepHoursHint': 'Rollback window; range 1–720',
  'ai.vmPolicy.denyTags': 'Write-protected tags',
  'ai.vmPolicy.denyTagsHint':
    'Comma separated. VMs carrying any of these tags can only be granted read-only access',
  'ai.vmPolicy.doubleApprove': 'Two-person approval for high-risk actions (optional)',
  'ai.vmPolicy.doubleApproveHint':
    'Optional and off by default; turn it on when you need it. When on, it only affects config changes, service restarts and higher-risk actions: those need approval from two different users with approval rights. Read-only and low-risk actions never need a second person, regardless of this switch. When off, it uses single-person approval — one approval is enough. If you are the only user with approval rights, approval automatically falls back to single-person mode instead of waiting until it times out.',
  'ai.vmPolicy.autoSnapshot': 'Snapshot before granting',
  'ai.vmPolicy.autoSnapshotHint':
    'Lets revoke or rollback restore the whole VM to its pre-grant state; falls back to file-level backups when snapshots are unsupported',
  'ai.vmPolicy.allowOps': 'Allow ops access (full guest shell)',
  'ai.vmPolicy.allowOpsHint':
    'When on, users can issue grants that install packages and edit any config. Every change still needs approval; removing the root filesystem, repartitioning and rebooting remain blocked server-side. Turning it off only affects newly issued grants',
  'ai.stop': 'Stop',
  'ai.remediation.title': 'Confirm remediation',
  'ai.remediation.previewing': 'Generating preview…',
  'ai.remediation.steps': 'Will run',
  'ai.remediation.risk.reversible': 'Reversible',
  'ai.remediation.risk.irreversible': 'Irreversible',
  'ai.remediation.irreversibleHint': 'This is irreversible (cuts network / powers off, etc.). Make sure the target is correct before running.',
  'ai.remediation.confirm': 'Run it',
  'ai.remediation.doneTitle': 'Remediation submitted',
  'ai.remediation.doneDetail': '“{label}” was executed; see the audit log for details.',
  'ai.abortedTitle': 'Inspection stopped',
  'ai.abortedHint': 'Stopped at the current step — no further model calls will be made',
  'ai.searchUser': 'Search username…',
  'ai.noMatchUser': 'No matching user',
  'ai.grantedCount': 'Granted to {n} user(s)',
  'ai.revokeGrant': 'Revoke access for {name}',
  'ai.connected': 'Connected',
  'ai.incomplete': 'Incomplete',
  'ai.connectFailed': 'Connection failed',
  'ai.badgeHint':
    '“Connected” only means the required fields are filled in — use Test connection to verify it actually works',
  'ai.grantTo': 'Grant to',
  'ai.grantHint':
    'Selected users can inspect with this model, but cannot see its key or modify it',
  'ai.noOtherUsers': 'No other users',
  'ai.grantBadge': 'granted',
  'ai.myModels': 'My models',
  'ai.noMyModel': "You haven't configured your own model yet",
  'ai.noMyModelHint':
    'Add your own model endpoint to run inspections — only you can use it, and only you are billed for it.',
  'ai.capSubtitle': 'What it can and cannot do',
  'ai.capIntro':
    'Pick a host: it first reads the inspection data the platform already has, then signs in to run commands when needed, and gives a conclusion backed by evidence. '
    + 'With a grant it can also work inside a VM: read-only, scoped changes, or ops access (can install packages) — you pick the level, and every step that changes the system needs your approval.',
  'ai.capKicker': 'What it does · when to use it',
  'ai.capToolsTitle': 'Tool list (filtered live by your permissions)',
  'ai.capSceneTitle': 'Typical scenarios',
  'ai.capSceneSlow': 'Slow service / intermittent 502',
  'ai.capSceneDisk': 'Disk full, log growth',
  'ai.capSceneCpu': 'CPU / memory anomalies',
  'ai.capSceneNetwork': 'Closed ports, network issues',
  'ai.capSceneInstall': 'Installing or upgrading software',
  'ai.capSceneConfig': 'Fixing a bad configuration',
  'ai.capFeature1Title': 'Host diagnosis and conclusions',
  'ai.capFeature1Desc':
    'Reads load, processes, ports and logs, then draws conclusions from the actual output instead of guessing at configs.',
  'ai.capFeature1Scene': 'Good for: slow responses, intermittent errors, unreachable ports.',
  'ai.capFeature2Title': 'Command execution (approved one by one)',
  'ai.capFeature2Desc':
    'Read-only diagnostics run directly; anything that mutates state shows the full command first and runs only after you approve. Output and exit code are audited.',
  'ai.capFeature2Scene': 'Good for: verifying a hypothesis on the box, not just reading logs.',
  'ai.capFeature3Title': 'In-guest investigation',
  'ai.capFeature3Desc':
    'Reads logs and configs inside the guest through the Guest Agent. The grant level defines the boundary: read-only, scoped changes, or full ops.',
  'ai.capFeature3Scene': 'Good for: guest-level faults invisible from the host.',
  'ai.capFeature4Title': 'Package install and service repair',
  'ai.capFeature4Desc':
    'Under an ops grant it runs as root to install or upgrade packages, edit any config and restart services; dangerous actions are confirmed one by one, while removing the root filesystem, repartitioning and rebooting are still refused server-side.',
  'ai.capFeature4Scene': 'Good for: missing dependencies, broken configs, stuck services.',
  'ai.capFeature5Title': 'Playbooks and follow-ups',
  'ai.capFeature5Desc':
    'Built-in playbooks run a full round of checks in one click; you can keep asking follow-ups and revisit past conversations at any time.',
  'ai.capFeature5Scene': 'Good for: walking the usual suspects quickly.',
  'ai.capFeature6Title': 'Audit and rollback',
  'ai.capFeature6Desc':
    'Every action is recorded; file writes back up the original, and write/ops grants take a snapshot at issue time so the VM can be rolled back.',
  'ai.capFeature6Scene': 'Good for: reviewing what changed, or undoing it in one go.',
  'ai.capInternalTitle': 'Platform data (read-only)',
  'ai.capInternalBody':
    'Never touches the host — reads inspection results the platform already collected.',
  'ai.capHostTitle': 'Runs on the target host',
  'ai.capHostBody':
    'It really signs in and runs commands: read-only diagnostics run directly; once “allow execution” is on, commands that change the system are proposed first and need your approval one at a time.',
  'ai.capGuestTitle': 'Inside the guest VM',
  'ai.capGuestBody':
    'Reaches into VMs you granted via the QEMU Guest Agent. The grant level defines what is possible: read-only, scoped changes (selected paths and units), or ops access (full shell, can install packages). Without a grant these tools are never sent to the model.',
  'ai.capLimitTitle': 'Limits',
  'ai.capLimitBody':
    'One inspection runs at most {steps} rounds within {seconds}s; every call is audited and can be reviewed one by one under “History”.',
  /* ---- Validation rules stay on the server: the page only says they exist ---- */
  'ai.capPolicyServerSide':
    'Command validation, path blocklists and approval rules live only on the server: read-only and scoped grants use an allowlist, while ops grants switch to per-change approval plus irreversible-action blocking. The rules are not enumerated here; every call is checked against the same policy and audited.',
  'ai.myModelsOnly':
    'You can only manage your own models here; shared ones are maintained by the admin',
  'ai.selectModel': 'Model',
  'ai.selectModelHint':
    'Applies to this inspection only; the default model set by the admin is unchanged',
  'ai.recExport': 'Export CSV',
  'ai.recExportHint':
    'Export inspection records with tool-call details; regular users export only their own records',
  'ai.stageQueued': 'Queued — waiting for a free slot…',
};
