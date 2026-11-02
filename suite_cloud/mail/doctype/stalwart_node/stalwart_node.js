// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Stalwart Node', {
	refresh(frm) {
		if (frm.doc.__islocal) return

		frm.add_custom_button(__('Verify SSH'), () => frm.events.call(frm, 'verify_ssh', __('Connecting...')), __('Actions'))
		if (frm.doc.ssh_host_keys) {
			frm.add_custom_button(
				__('Reset SSH Host Keys'),
				() =>
					frappe.confirm(
						__('Forget the recorded host keys of {0}? Only do this when the server was reinstalled; the next Verify SSH trusts whatever server answers at {1}.', [frm.doc.hostname, frm.doc.ipv4_address]),
						() => frm.events.call(frm, 'reset_ssh_host_keys', __('Resetting...')),
					),
				__('Actions'),
			)
		}

		if (['Pending', 'Failed'].includes(frm.doc.status)) {
			frm.add_custom_button(__('Provision'), () => frm.events.call(frm, 'provision', __('Queueing provisioning...')), __('Actions'))
		}
		if (['Provisioned', 'Active', 'Draining'].includes(frm.doc.status)) {
			// Idempotent: re-applies a changed plan or environment and restarts Stalwart.
			frm.add_custom_button(
				__('Re-provision'),
				() =>
					frappe.confirm(__('Run the provisioning playbook again? Stalwart restarts on the node.'), () =>
						frm.events.call(frm, 'provision', __('Queueing provisioning...')),
					),
				__('Actions'),
			)
		}
		if (frm.doc.status === 'Provisioned') {
			frm.add_custom_button(__('Check Health'), () => frm.events.call(frm, 'check_health', __('Checking...')), __('Actions'))
		}
		if (frm.doc.status === 'Active') {
			frm.add_custom_button(__('Drain'), () => frm.events.call(frm, 'drain', __('Removing from ingress DNS...')), __('Actions'))
			frm.add_custom_button(__('Upgrade'), () => frm.events.call(frm, 'upgrade', __('Queueing upgrade...')), __('Actions'))
		}
		if (['Provisioned', 'Active', 'Draining'].includes(frm.doc.status)) {
			frm.add_custom_button(__('Install Agent'), () => frm.events.call(frm, 'install_agent', __('Queueing the agent install...')), __('Actions'))
			frm.add_custom_button(__('Show Agent Config'), () => frm.events.show_agent_config(frm), __('Actions'))
			frm.add_custom_button(
				__('Rotate Telemetry Token'),
				() =>
					frappe.confirm(__('Replace the telemetry secret of {0}? A job writes it to the node and restarts Stalwart.', [frm.doc.hostname]), () =>
						frm.events.call(frm, 'rotate_telemetry_token', __('Queueing...')),
					),
				__('Actions'),
			)
		}
		if (frm.doc.status === 'Draining') {
			frm.add_custom_button(__('Restore'), () => frm.events.call(frm, 'restore', __('Adding to ingress DNS...')), __('Actions'))
		}
		frm.add_custom_button(__('Verify PTR'), () => frm.events.call(frm, 'verify_ptr', __('Resolving...')), __('Actions'))

		if (!frm.doc.ssh_verified) {
			frm.dashboard.add_comment(
				__("Add the cluster's SSH public key to this VPS, then run Verify SSH."),
				'yellow',
				true,
			)
		}
		if (['Provisioned', 'Active', 'Draining'].includes(frm.doc.status)) frm.events.show_telemetry_status(frm)
	},

	show_telemetry_status(frm) {
		frappe.xcall('suite_cloud.telemetry.status.get_server_status', { doctype: frm.doctype, name: frm.doc.name }).then((status) => {
			const colors = { ok: 'green', silent: 'red', unknown: 'blue' }
			frm.dashboard.add_comment(frappe.utils.escape_html(status.message), colors[status.state], true)
		})
	},

	show_agent_config(frm) {
		frm.call({ doc: frm.doc, method: 'show_agent_config' }).then(({ message }) => {
			const note = message.telemetry_enabled
				? __('What the next Install Agent writes to {0}. Secrets are masked.', ['<code>/etc/suite-cloud-agent/config.json</code>'])
				: __('Enable Telemetry is off: Install Agent would stop the agent instead of writing this file.')
			const config = frappe.utils.escape_html(JSON.stringify(message.config, null, 2))
			frappe.msgprint({ title: __('Agent Config'), wide: true, message: `<p class="text-muted">${note}</p><pre>${config}</pre>` })
		})
	},

	call(frm, method, freeze_message) {
		frappe.call({
			doc: frm.doc,
			method,
			freeze: true,
			freeze_message,
			callback: (r) => {
				if (!r.exc) frm.reload_doc()
			},
		})
	},
})
