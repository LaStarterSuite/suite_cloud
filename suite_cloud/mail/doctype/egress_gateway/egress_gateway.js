// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Egress Gateway', {
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
		frm.add_custom_button(__('Preview Plan'), () => frm.events.call(frm, 'preview_plan', __('Rendering...'), (plan) => {
			frappe.msgprint({ title: __('Gateway Plan'), message: `<pre>${frappe.utils.escape_html(plan)}</pre>`, wide: true })
		}), __('Actions'))
		if (['Pending', 'Failed'].includes(frm.doc.status)) {
			frm.add_custom_button(__('Provision'), () => frm.events.call(frm, 'provision', __('Queueing...')), __('Actions'))
		}
		if (['Provisioned', 'Active'].includes(frm.doc.status)) {
			frm.events.show_telemetry_status(frm)
			frm.add_custom_button(__('Rotate Suite Cloud Token'), () => frm.events.rotate_suite_cloud_token(frm), __('Actions'))
			frm.add_custom_button(
				__('Re-provision'),
				() =>
					frappe.confirm(__('Run the provisioning playbook again? Stalwart restarts on the gateway.'), () =>
						frm.events.call(frm, 'provision', __('Queueing...')),
					),
				__('Actions'),
			)
		}
		if (frm.doc.status === 'Provisioned') {
			frm.add_custom_button(__('Check Health'), () => frm.events.call(frm, 'check_health', __('Checking...')), __('Actions'))
		}
		if (frm.doc.status === 'Active') {
			frm.add_custom_button(__('Sync Config'), () => frm.events.call(frm, 'sync_config', __('Syncing...')), __('Actions'))
			frm.add_custom_button(__('Upgrade'), () => frm.events.call(frm, 'upgrade', __('Queueing...')), __('Actions'))
			frm.add_custom_button(
				__('Replace DKIM Keys'),
				() =>
					frappe.confirm(__('Generate new DKIM keys for {0}? Without a DNS provider on the zone the new records must be published by hand.', [frm.doc.hostname]), () =>
						frm.events.call(frm, 'replace_dkim_keys', __('Replacing keys...')),
					),
				__('Actions'),
			)
		}
		if (frappe.session.user === 'Administrator') {
			frm.add_custom_button(__('Show Admin Password'), () => frm.trigger('show_admin_password'), __('Access'))
		}
	},

	show_admin_password(frm) {
		frappe.call({
			doc: frm.doc,
			method: 'show_admin_password',
			freeze: true,
			freeze_message: __('Fetching...'),
			callback: (r) => {
				if (r.exc) return
				frappe.msgprint({
					title: __('Admin Password'),
					message: `${__('Web admin')}: <a href="${frm.doc.base_url}" target="_blank">${frm.doc.base_url}</a><br>${__('Username')}: <code>${frappe.utils.escape_html(frm.doc.admin_username)}</code><br>${__('Password')}: <code>${frappe.utils.escape_html(r.message)}</code>`,
				})
			},
		})
	},

	show_telemetry_status(frm) {
		// One indicator per kind of telemetry; the server decides the state and the color.
		frappe.xcall('suite_cloud.telemetry.status.get_server_status', { doctype: frm.doctype, name: frm.doc.name }).then((status) => {
			for (const item of status.indicators) frm.dashboard.add_indicator(frappe.utils.escape_html(item.text), item.color)
		})
	},

	rotate_suite_cloud_token(frm) {
		frappe.confirm(__('Replace the Suite Cloud secret of {0}? A job writes it to the server and restarts Stalwart.', [frm.doc.hostname]), () =>
			frm.events.call(frm, 'rotate_suite_cloud_token', __('Queueing...')),
		)
	},

	call(frm, method, freeze_message, done) {
		frappe.call({
			doc: frm.doc,
			method,
			freeze: true,
			freeze_message,
			callback: (r) => {
				if (r.exc) return
				frm.reload_doc()
				if (done) done(r.message)
			},
		})
	},
})
