// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Mail Account', {
	refresh(frm) {
		if (frm.doc.__islocal) return
		frm.add_custom_button(__('Reset Password'), () => frm.trigger('reset_password'), __('Credentials'))
		frm.add_custom_button(__('Rotate App Password'), () => frm.trigger('rotate_app_password'), __('Credentials'))
		frm.add_custom_button(__('Rotate API Key'), () => frm.trigger('rotate_api_key'), __('Credentials'))
		frm.trigger('show_send_usage')
	},

	tenant(frm) {
		// The tenant follows the domain; a new account starts at its default limits.
		if (!frm.is_new() || !frm.doc.tenant) return
		const fields = ['default_daily_send_limit', 'default_monthly_send_limit']
		frappe.db.get_value('Suite Tenant', frm.doc.tenant, fields).then(({ message }) => {
			if (!frm.doc.daily_send_limit) frm.set_value('daily_send_limit', message.default_daily_send_limit)
			if (!frm.doc.monthly_send_limit) frm.set_value('monthly_send_limit', message.default_monthly_send_limit)
		})
	},

	show_send_usage(frm) {
		const usage = frm.doc.__onload?.send_usage
		if (!usage) return
		const part = (label, sent, limit) => {
			const text = limit ? `${format_number(sent, null, 0)} / ${format_number(limit, null, 0)}` : format_number(sent, null, 0)
			return { text: `${label}: ${text}`, ratio: limit ? sent / limit : 0 }
		}
		const parts = [
			part(__('Sent today'), usage.day, frm.doc.daily_send_limit),
			part(__('This month'), usage.month, frm.doc.monthly_send_limit),
		]
		const worst = Math.max(...parts.map((p) => p.ratio))
		const color = worst >= 1 ? 'red' : worst >= 0.8 ? 'orange' : 'blue'
		frm.dashboard.add_indicator(parts.map((p) => p.text).join(' · '), color)
		if (usage.refused_day || usage.refused_month) {
			const refused = __('Recipients refused over a limit: {0} today · {1} this month', [
				format_number(usage.refused_day, null, 0),
				format_number(usage.refused_month, null, 0),
			])
			frm.dashboard.add_indicator(refused, 'red')
		}
	},

	reset_password(frm) {
		const dialog = new frappe.ui.Dialog({
			title: __('Reset Password'),
			fields: [
				{
					fieldname: 'password',
					fieldtype: 'Password',
					label: __('New Password'),
					description: __('Leave blank to generate one. At least 8 characters.'),
				},
			],
			primary_action_label: __('Reset'),
			primary_action: ({ password }) => {
				dialog.hide()
				frm.events.call(frm, 'reset_password', { password }, __('Updating the cluster...'), (result) => {
					frm.events.reveal(__('New Password'), result, __('Shown once; hand it to the user now.'))
				})
			},
		})
		dialog.show()
	},

	rotate_app_password(frm) {
		frappe.confirm(__('Mint a new app password for this account and revoke the current one? The tenant must be given the new one.'), () => {
			frm.events.call(frm, 'rotate_app_password', {}, __('Rotating...'), (secret) => {
				frm.events.reveal(__('App Password'), secret, __('Shown once; the previous app password no longer works.'))
			})
		})
	},

	rotate_api_key(frm) {
		frappe.confirm(__('Mint a new API key for this account and revoke the current one?'), () => {
			frm.events.call(frm, 'rotate_api_key', {}, __('Rotating...'), (key) => {
				frm.events.reveal(__('API Key'), key, __('Shown once; the previous key no longer works.'))
			})
		})
	},

	reveal(title, secret, note) {
		frappe.msgprint({
			title,
			message: `<code>${frappe.utils.escape_html(secret)}</code>${note ? `<p class="mt-2 text-muted">${note}</p>` : ''}`,
		})
	},

	call(frm, method, args, freeze_message, done) {
		frappe.call({
			doc: frm.doc,
			method,
			args,
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
