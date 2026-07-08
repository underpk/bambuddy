import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  CircleAlert,
  ExternalLink,
  Plus,
  RefreshCw,
  Settings2,
  ShoppingBag,
  Trash2,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api, type ShopeeMapping, type ShopeeOrder } from '../api/client';
import { Card, CardContent, CardHeader } from '../components/Card';
import { Button } from '../components/Button';
import { useToast } from '../contexts/ToastContext';

const STATUS_STYLES: Record<string, string> = {
  new: 'bg-bambu-green/20 text-bambu-green',
  queued: 'bg-blue-500/20 text-blue-400',
  manual: 'bg-amber-500/20 text-amber-400',
  cancel_requested: 'bg-red-500/20 text-red-400',
  shipped: 'bg-purple-500/20 text-purple-400',
  done: 'bg-gray-500/20 text-gray-400',
};

const ORDER_STATUSES = ['new', 'queued', 'manual', 'cancel_requested', 'shipped', 'done'];

function StatusChip({ status }: { status: string }) {
  const { t } = useTranslation();
  return (
    <span
      className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium whitespace-nowrap ${STATUS_STYLES[status] ?? STATUS_STYLES.done}`}
    >
      {t(`shopee.status.${status}`, status.replace('_', ' '))}
    </span>
  );
}

function daysUntil(iso: string | null): number | null {
  if (!iso) return null;
  return Math.ceil((new Date(iso).getTime() - Date.now()) / 86_400_000);
}

function DeliverBy({ order }: { order: ShopeeOrder }) {
  const days = daysUntil(order.deliver_by);
  if (!order.deliver_by_raw) return <span className="text-bambu-gray">—</span>;
  const urgent = days !== null && days <= 3;
  return (
    <span className={`whitespace-nowrap ${urgent ? 'text-red-400 font-medium' : 'text-bambu-gray-light'}`}>
      {urgent && <CircleAlert className="w-3.5 h-3.5 inline mr-1 -mt-0.5" />}
      {order.deliver_by_raw}
      {days !== null && <span className="text-xs opacity-75"> ({days}d)</span>}
    </span>
  );
}

// ─── Settings panel ───────────────────────────────────────────────────────────

function ShopeeSettingsPanel() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const { data: settings } = useQuery({ queryKey: ['shopee-settings'], queryFn: () => api.getShopeeSettings() });

  const [form, setForm] = useState<{
    enabled: boolean;
    imap_host: string;
    imap_user: string;
    imap_password: string;
    poll_interval_minutes: number;
    since_days: number;
  } | null>(null);

  const effective = form ?? {
    enabled: settings?.enabled ?? false,
    imap_host: settings?.imap_host ?? 'imap.gmail.com',
    imap_user: settings?.imap_user ?? '',
    imap_password: '',
    poll_interval_minutes: settings?.poll_interval_minutes ?? 5,
    since_days: settings?.since_days ?? 7,
  };

  const saveMutation = useMutation({
    mutationFn: () => api.saveShopeeSettings(effective),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['shopee-settings'] });
      setForm((f) => (f ? { ...f, imap_password: '' } : f));
      showToast(t('shopee.settingsSaved', 'Shopee settings saved'), 'success');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const set = (patch: Partial<typeof effective>) => setForm({ ...effective, ...patch });

  const inputCls =
    'w-full px-3 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white placeholder-bambu-gray focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors';

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Settings2 className="w-5 h-5 text-bambu-green" />
          <h3 className="text-white font-semibold">{t('shopee.emailSettings', 'Order email polling')}</h3>
        </div>
      </CardHeader>
      <CardContent>
        <div className="space-y-4">
          <p className="text-bambu-gray text-sm">
            {t(
              'shopee.emailSettingsHint',
              'Bambuddy polls this mailbox for Shopee seller notification emails. For Gmail, create an App Password (Google Account → Security → 2-Step Verification → App passwords) — your normal password will not work.'
            )}
          </p>
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={effective.enabled}
              onChange={(e) => set({ enabled: e.target.checked })}
              className="w-4 h-4 accent-bambu-green"
            />
            <span className="text-white text-sm">{t('shopee.pollingEnabled', 'Automatic polling enabled')}</span>
          </label>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="text-bambu-gray text-xs block mb-1">{t('shopee.imapHost', 'IMAP host')}</label>
              <input className={inputCls} value={effective.imap_host} onChange={(e) => set({ imap_host: e.target.value })} />
            </div>
            <div>
              <label className="text-bambu-gray text-xs block mb-1">{t('shopee.imapUser', 'Email address')}</label>
              <input className={inputCls} value={effective.imap_user} onChange={(e) => set({ imap_user: e.target.value })} placeholder="you@gmail.com" />
            </div>
            <div>
              <label className="text-bambu-gray text-xs block mb-1">
                {t('shopee.imapPassword', 'App password')}
                {settings?.password_set && (
                  <span className="text-bambu-green ml-2">{t('shopee.passwordSet', '(set — leave blank to keep)')}</span>
                )}
              </label>
              <input
                type="password"
                className={inputCls}
                value={effective.imap_password}
                onChange={(e) => set({ imap_password: e.target.value })}
                placeholder="xxxx xxxx xxxx xxxx"
                autoComplete="new-password"
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-bambu-gray text-xs block mb-1">{t('shopee.pollInterval', 'Interval (min)')}</label>
                <input
                  type="number"
                  min={1}
                  max={1440}
                  className={inputCls}
                  value={effective.poll_interval_minutes}
                  onChange={(e) => set({ poll_interval_minutes: Number(e.target.value) || 5 })}
                />
              </div>
              <div>
                <label className="text-bambu-gray text-xs block mb-1">{t('shopee.sinceDays', 'Look back (days)')}</label>
                <input
                  type="number"
                  min={1}
                  max={60}
                  className={inputCls}
                  value={effective.since_days}
                  onChange={(e) => set({ since_days: Number(e.target.value) || 7 })}
                />
              </div>
            </div>
          </div>
          <Button onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending}>
            {saveMutation.isPending ? t('common.saving') : t('common.save')}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ─── Mappings panel ───────────────────────────────────────────────────────────

function ShopeeMappingsPanel() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const { data: mappings } = useQuery({ queryKey: ['shopee-mappings'], queryFn: () => api.getShopeeMappings() });
  const { data: libraryFiles } = useQuery({
    queryKey: ['shopee-library-files'],
    queryFn: () => api.getLibraryFiles(null, true, undefined, undefined, true),
  });

  const printableFiles = useMemo(
    () => (libraryFiles ?? []).filter((f) => /\.(3mf|gcode)(\.|$)/i.test(f.filename)),
    [libraryFiles]
  );

  const [matchName, setMatchName] = useState('');
  const [matchVariation, setMatchVariation] = useState('');
  const [fileId, setFileId] = useState<number | ''>('');
  const [copies, setCopies] = useState(1);
  const [autoQueue, setAutoQueue] = useState(true);

  const createMutation = useMutation({
    mutationFn: () =>
      api.createShopeeMapping({
        match_name: matchName.trim(),
        match_variation: matchVariation.trim() || null,
        library_file_id: fileId as number,
        copies_per_unit: copies,
        auto_queue: autoQueue,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['shopee-mappings'] });
      setMatchName('');
      setMatchVariation('');
      setFileId('');
      setCopies(1);
      showToast(t('shopee.mappingAdded', 'Mapping added'), 'success');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteShopeeMapping(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['shopee-mappings'] }),
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const inputCls =
    'w-full px-3 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white placeholder-bambu-gray focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors';

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <ShoppingBag className="w-5 h-5 text-bambu-green" />
          <h3 className="text-white font-semibold">{t('shopee.mappings', 'Product → print file mappings')}</h3>
        </div>
      </CardHeader>
      <CardContent>
        <div className="space-y-4">
          <p className="text-bambu-gray text-sm">
            {t(
              'shopee.mappingsHint',
              'When an incoming order item contains the product text (and variation, if set), the linked library file is queued automatically — jobs are created with manual start, so nothing prints unattended.'
            )}
          </p>

          {mappings && mappings.length > 0 && (
            <div className="space-y-2">
              {mappings.map((m: ShopeeMapping) => (
                <div key={m.id} className="flex items-center justify-between bg-bambu-dark-secondary rounded-lg px-4 py-3">
                  <div className="min-w-0">
                    <p className="text-white text-sm font-medium truncate">
                      “{m.match_name}”{m.match_variation ? ` · ${m.match_variation}` : ''}
                    </p>
                    <p className="text-bambu-gray text-xs truncate">
                      → {m.library_file_name ?? `file #${m.library_file_id}`} ×{m.copies_per_unit}
                      {m.auto_queue
                        ? ` · ${t('shopee.autoQueueOn', 'auto-queue')}`
                        : ` · ${t('shopee.autoQueueOff', 'annotate only')}`}
                    </p>
                  </div>
                  <Button variant="ghost" size="sm" onClick={() => deleteMutation.mutate(m.id)}>
                    <Trash2 className="w-4 h-4 text-red-400" />
                  </Button>
                </div>
              ))}
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <input
              className={inputCls}
              value={matchName}
              onChange={(e) => setMatchName(e.target.value)}
              placeholder={t('shopee.matchName', 'Product name contains… (e.g. Melodrip Tray)')}
            />
            <input
              className={inputCls}
              value={matchVariation}
              onChange={(e) => setMatchVariation(e.target.value)}
              placeholder={t('shopee.matchVariation', 'Variation contains… (optional)')}
            />
            <select className={inputCls} value={fileId} onChange={(e) => setFileId(e.target.value ? Number(e.target.value) : '')}>
              <option value="">{t('shopee.pickFile', 'Choose library file…')}</option>
              {printableFiles.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.filename}
                </option>
              ))}
            </select>
            <div className="flex items-center gap-4">
              <div className="flex items-center gap-2">
                <span className="text-bambu-gray text-xs">{t('shopee.copies', 'Copies/unit')}</span>
                <input
                  type="number"
                  min={1}
                  max={50}
                  className={`${inputCls} w-20`}
                  value={copies}
                  onChange={(e) => setCopies(Math.max(1, Number(e.target.value) || 1))}
                />
              </div>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={autoQueue} onChange={(e) => setAutoQueue(e.target.checked)} className="w-4 h-4 accent-bambu-green" />
                <span className="text-white text-sm">{t('shopee.autoQueue', 'Auto-queue')}</span>
              </label>
            </div>
          </div>
          <Button
            size="sm"
            disabled={!matchName.trim() || fileId === '' || createMutation.isPending}
            onClick={() => createMutation.mutate()}
            className="flex items-center gap-2"
          >
            <Plus className="w-4 h-4" />
            {t('shopee.addMapping', 'Add mapping')}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

export function ShopeeOrdersPage() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [showConfig, setShowConfig] = useState(false);

  const { data: orders, isLoading } = useQuery({
    queryKey: ['shopee-orders', statusFilter],
    queryFn: () => api.getShopeeOrders(statusFilter || undefined),
    refetchInterval: 60_000,
  });

  const syncMutation = useMutation({
    mutationFn: () => api.shopeeSyncNow(),
    onSuccess: (r) => {
      queryClient.invalidateQueries({ queryKey: ['shopee-orders'] });
      showToast(
        t('shopee.syncResult', {
          defaultValue: 'Scanned {{scanned}} emails: {{orders}} new orders, {{jobs}} print jobs queued',
          scanned: r.emails_scanned,
          orders: r.new_orders,
          jobs: r.print_jobs_queued,
        }),
        'success'
      );
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, status }: { id: number; status: string }) => api.updateShopeeOrder(id, { status }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['shopee-orders'] }),
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const newCount = (orders ?? []).filter((o) => o.status === 'new').length;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <ShoppingBag className="w-6 h-6 text-bambu-green" />
        <h1 className="text-xl font-bold text-white">{t('shopee.title', 'Shopee Orders')}</h1>
        {newCount > 0 && (
          <span className="bg-bambu-green text-white text-xs font-bold rounded-full px-2.5 py-1">
            {t('shopee.newCount', { defaultValue: '{{count}} new', count: newCount })}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="px-3 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none"
          >
            <option value="">{t('shopee.allStatuses', 'All statuses')}</option>
            {ORDER_STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`shopee.status.${s}`, s.replace('_', ' '))}
              </option>
            ))}
          </select>
          <Button variant="secondary" size="sm" onClick={() => setShowConfig((v) => !v)} className="flex items-center gap-2">
            <Settings2 className="w-4 h-4" />
            {t('shopee.configure', 'Configure')}
          </Button>
          <Button size="sm" onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending} className="flex items-center gap-2">
            <RefreshCw className={`w-4 h-4 ${syncMutation.isPending ? 'animate-spin' : ''}`} />
            {t('shopee.syncNow', 'Sync now')}
          </Button>
        </div>
      </div>

      {showConfig && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <ShopeeSettingsPanel />
          <ShopeeMappingsPanel />
        </div>
      )}

      <Card>
        <CardContent>
          {isLoading ? (
            <p className="text-bambu-gray text-sm py-8 text-center">{t('common.loading')}</p>
          ) : !orders || orders.length === 0 ? (
            <div className="py-12 text-center space-y-2">
              <ShoppingBag className="w-10 h-10 text-bambu-gray mx-auto" />
              <p className="text-bambu-gray">{t('shopee.empty', 'No orders yet — configure email polling and press Sync now.')}</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-bambu-gray text-left border-b border-bambu-dark-tertiary">
                    <th className="py-2 pr-4 font-medium">{t('shopee.order', 'Order')}</th>
                    <th className="py-2 pr-4 font-medium">{t('shopee.buyer', 'Buyer')}</th>
                    <th className="py-2 pr-4 font-medium">{t('shopee.items', 'Items')}</th>
                    <th className="py-2 pr-4 font-medium text-right">{t('shopee.total', 'Total')}</th>
                    <th className="py-2 pr-4 font-medium">{t('shopee.deliverBy', 'Deliver by')}</th>
                    <th className="py-2 pr-4 font-medium">{t('shopee.statusLabel', 'Status')}</th>
                    <th className="py-2 font-medium"></th>
                  </tr>
                </thead>
                <tbody>
                  {orders.map((order) => (
                    <tr key={order.id} className="border-b border-bambu-dark-tertiary/50 align-top">
                      <td className="py-3 pr-4 whitespace-nowrap">
                        {order.seller_center_url ? (
                          <a
                            href={order.seller_center_url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-bambu-green hover:underline inline-flex items-center gap-1"
                          >
                            #{order.order_sn}
                            <ExternalLink className="w-3 h-3" />
                          </a>
                        ) : (
                          <span className="text-white">#{order.order_sn}</span>
                        )}
                        <p className="text-bambu-gray text-xs">
                          {order.order_date ? new Date(order.order_date).toLocaleString() : ''}
                        </p>
                      </td>
                      <td className="py-3 pr-4 text-bambu-gray-light">{order.buyer_username ?? '—'}</td>
                      <td className="py-3 pr-4">
                        {order.items.length === 0 ? (
                          <span className="text-bambu-gray">—</span>
                        ) : (
                          <ul className="space-y-1">
                            {order.items.map((item) => (
                              <li key={item.id} className="text-bambu-gray-light">
                                <span className="text-white">{item.quantity}×</span> {item.name}
                                {item.variation && <span className="text-bambu-gray"> · {item.variation}</span>}
                                {item.queued_count > 0 && (
                                  <span className="text-blue-400 text-xs ml-1">
                                    ({t('shopee.queuedJobs', { defaultValue: '{{count}} queued', count: item.queued_count })})
                                  </span>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}
                      </td>
                      <td className="py-3 pr-4 text-right text-white whitespace-nowrap">
                        {order.total != null ? `฿${order.total.toLocaleString()}` : '—'}
                      </td>
                      <td className="py-3 pr-4">
                        <DeliverBy order={order} />
                      </td>
                      <td className="py-3 pr-4">
                        <StatusChip status={order.status} />
                      </td>
                      <td className="py-3">
                        <select
                          value={order.status}
                          onChange={(e) => updateMutation.mutate({ id: order.id, status: e.target.value })}
                          className="px-2 py-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-bambu-gray-light text-xs focus:outline-none"
                        >
                          {ORDER_STATUSES.map((s) => (
                            <option key={s} value={s}>
                              {t(`shopee.status.${s}`, s.replace('_', ' '))}
                            </option>
                          ))}
                        </select>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
