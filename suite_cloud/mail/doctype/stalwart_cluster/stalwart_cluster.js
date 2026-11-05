// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Stalwart Cluster', {
	refresh(frm) {
		frm.trigger('set_store_queries')
		frm.trigger('add_actions')
		frm.trigger('add_indicators')
	},

	set_store_queries(frm) {
		const kinds = {
			data_store: 'Data',
			blob_store: 'Blob',
			search_store: 'Search',
			in_memory_store: 'In-Memory',
		}
		for (const [field, kind] of Object.entries(kinds)) {
			frm.set_query(field, () => ({ filters: { kind } }))
		}
		frm.set_query('default_egress_pool', () => ({ filters: { cluster: frm.doc.name } }))
	},

	add_actions(frm) {
		if (frm.doc.__islocal) return

		frm.add_custom_button(__('Preview Plan'), () => frm.trigger('preview_plan'), __('Configuration'))

		if (frm.doc.status === 'Active') {
			frm.add_custom_button(__('Sync Config'), () => frm.trigger('sync_config'), __('Configuration'))
			frm.add_custom_button(__('Check Drift'), () => frm.trigger('check_drift'), __('Configuration'))
			frm.add_custom_button(__('Reconcile Directory'), () => frm.trigger('reconcile_directory'), __('Configuration'))
			frm.add_custom_button(__('Rotate API Key'), () => frm.trigger('rotate_api_key'), __('Access'))
			frm.add_custom_button(__('Replace DKIM Keys'), () => frm.trigger('replace_dkim_keys'), __('Access'))
			frm.add_custom_button(__('Upgrade Nodes'), () => frm.trigger('upgrade_nodes'), __('Nodes'))
			frm.add_custom_button(__('Install Suite Cloud Tokens'), () => frm.trigger('install_suite_cloud_tokens'), __('Nodes'))
			frm.add_custom_button(__('Install Outbound Scanners'), () => frm.trigger('install_outbound_scanners'), __('Nodes'))
		}
		if (frm.doc.status === 'Bootstrapping') {
			frm.add_custom_button(__('Finish Bootstrap'), () => frm.trigger('finish_bootstrap'), __('Nodes'))
		}
		if (frappe.session.user === 'Administrator') {
			frm.add_custom_button(__('Show Admin Password'), () => frm.trigger('show_admin_password'), __('Access'))
		}
	},

	add_indicators(frm) {
		if (frm.doc.__islocal) return
		if (frm.doc.single_node) {
			frm.dashboard.add_comment(
				__('A store is embedded (RocksDB or local files), so this cluster can only ever have one node. Use PostgreSQL or MySQL, S3 and Redis stores for a cluster that can grow.'),
				'orange',
				true,
			)
		}
		if (frm.doc.status === 'Pending') {
			frm.dashboard.add_comment(
				__('Add the cluster SSH public key to the first node VPS, create a Stalwart Node and provision it.'),
				'blue',
				true,
			)
		}
		if (frm.doc.status === 'Active') {
			frappe.xcall('suite_cloud.telemetry.status.get_cluster_summary', { cluster: frm.doc.name }).then((summary) => {
				if (!summary.message) return
				const colors = { ok: 'green', silent: 'red', unknown: 'blue' }
				frm.dashboard.add_comment(frappe.utils.escape_html(summary.message), colors[summary.state], true)
			})
		}
		const outdated = (frm.doc.__onload || {}).outdated_scanner_nodes || []
		if (outdated.length) {
			frm.dashboard.add_comment(
				__('The outbound scanner of {0} does not have the current settings. Run Install Outbound Scanners.', [
					outdated.map((node) => frappe.utils.escape_html(node)).join(', '),
				]),
				'orange',
				true,
			)
		}
		if (frm.doc.last_config_sync_error) {
			frm.dashboard.add_comment(
				__('The last configuration push failed: {0}. Sync Config tries again.', [
					frappe.utils.escape_html(frm.doc.last_config_sync_error),
				]),
				'red',
				true,
			)
		}
		if (frm.doc.drift_report) {
			const report = JSON.parse(frm.doc.drift_report)
			if (report.differences && report.differences.length) {
				frm.dashboard.add_comment(
					__('Configuration drift detected ({0} differences). Sync Config restores the generated state.', [
						report.differences.length,
					]),
					'orange',
					true,
				)
			}
		}
	},

	call(frm, method, args, freeze_message, done) {
		frappe.call({
			doc: frm.doc,
			method,
			args,
			freeze: true,
			freeze_message,
			callback: (r) => {
				if (!r.exc) {
					frm.reload_doc()
					if (done) done(r.message)
				}
			},
		})
	},

	preview_plan(frm) {
		frm.events.call(frm, 'preview_plan', {}, __('Rendering plan...'), (plan) => {
			frappe.msgprint({ title: __('Cluster Plan'), message: `<pre>${frappe.utils.escape_html(plan)}</pre>`, wide: true })
		})
	},

	sync_config(frm) {
		frappe.confirm(__('Push the generated configuration to the running cluster?'), () => {
			frm.events.call(frm, 'sync_config', {}, __('Syncing configuration...'), (r) => {
				frappe.show_alert({ message: __('Synced: {0} updated', [r.updated.length]), indicator: 'green' })
			})
		})
	},

	check_drift(frm) {
		frm.events.call(frm, 'check_drift', {}, __('Comparing with the cluster...'))
	},

	reconcile_directory(frm) {
		frm.events.call(frm, 'reconcile_directory', {}, __('Comparing directories...'), (report) => {
			frappe.msgprint({ title: __('Directory Report'), message: `<pre>${frappe.utils.escape_html(JSON.stringify(report, null, 2))}</pre>`, wide: true })
		})
	},

	finish_bootstrap(frm) {
		frm.events.call(frm, 'finish_bootstrap', {}, __('Checking the bootstrap node...'), (done) => {
			frappe.show_alert({
				message: done ? __('Cluster is active') : __('Not ready yet; the certificate may still be pending.'),
				indicator: done ? 'green' : 'orange',
			})
		})
	},

	rotate_api_key(frm) {
		frappe.confirm(__('Mint a new Stalwart API key and revoke the current one?'), () => {
			frm.events.call(frm, 'rotate_api_key', {}, __('Rotating API key...'))
		})
	},

	replace_dkim_keys(frm) {
		frappe.confirm(__('Generate new DKIM keys for {0}? Without a DNS provider on the zone the new records must be published by hand.', [frm.doc.default_domain]), () => {
			frm.events.call(frm, 'replace_dkim_keys', {}, __('Replacing keys...'))
		})
	},

	upgrade_nodes(frm) {
		frappe.confirm(__('Upgrade every active node to {0}, one at a time?', [frm.doc.stalwart_version]), () => {
			frm.events.call(frm, 'upgrade_nodes', {}, __('Queueing upgrades...'))
		})
	},

	install_suite_cloud_tokens(frm) {
		frappe.confirm(__('Install the Suite Cloud token on every active or draining node and every active gateway? Stalwart restarts on a server whose token changed. Once all of them have it, the config is pushed, and Stalwart restarts on every server to start the metrics push while telemetry is on.'), () => {
			frm.events.call(frm, 'install_suite_cloud_tokens', {}, __('Queueing token installs...'))
		})
	},

	install_outbound_scanners(frm) {
		frappe.confirm(__('Install Rspamd, the outbound spam scanner, with the current settings on every running node? Once all of them have it, the config is pushed and the scan starts.'), () => {
			frm.events.call(frm, 'install_outbound_scanners', {}, __('Queueing scanner installs...'))
		})
	},

	show_admin_password(frm) {
		frm.events.call(frm, 'show_admin_password', {}, __('Fetching...'), (password) => {
			frappe.msgprint({ title: __('Admin Password'), message: `<code>${frappe.utils.escape_html(password)}</code>` })
		})
	},
})
