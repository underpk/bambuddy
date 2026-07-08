import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Fingerprint, Plus, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api, type WebAuthnCredentialInfo } from '../api/client';
import { Card, CardContent, CardHeader } from './Card';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';
import { createPasskey, isWebAuthnSupported } from '../utils/webauthn';

function formatDate(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString();
}

export function PasskeySettings() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [deviceName, setDeviceName] = useState('');
  const [showAdd, setShowAdd] = useState(false);

  const { data: credentials } = useQuery({
    queryKey: ['webauthn-credentials'],
    queryFn: () => api.webauthnListCredentials(),
  });

  const registerMutation = useMutation({
    mutationFn: async () => {
      const { options } = await api.webauthnRegisterBegin();
      const credential = await createPasskey(options);
      return api.webauthnRegisterComplete(credential, deviceName || undefined);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['webauthn-credentials'] });
      queryClient.invalidateQueries({ queryKey: ['webauthnStatus'] });
      setDeviceName('');
      setShowAdd(false);
      showToast(t('settings.passkeys.added', 'Passkey added'), 'success');
    },
    onError: (error: Error) => {
      if (error.name !== 'NotAllowedError') {
        showToast(error.message || t('settings.passkeys.addFailed', 'Could not add passkey'), 'error');
      }
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.webauthnDeleteCredential(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['webauthn-credentials'] });
      queryClient.invalidateQueries({ queryKey: ['webauthnStatus'] });
      showToast(t('settings.passkeys.removed', 'Passkey removed'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const supported = isWebAuthnSupported();

  return (
    <Card id="card-passkeys">
      <CardHeader>
        <div className="flex items-center gap-3">
          <div className={`w-10 h-10 rounded-full flex items-center justify-center ${credentials?.length ? 'bg-green-500/20' : 'bg-gray-500/20'}`}>
            <Fingerprint className={`w-5 h-5 ${credentials?.length ? 'text-green-400' : 'text-gray-400'}`} />
          </div>
          <div>
            <h3 className="text-white font-semibold">{t('settings.passkeys.title', 'Passkeys')}</h3>
            <p className="text-bambu-gray text-sm">
              {t('settings.passkeys.desc', 'Sign in with fingerprint, face, or device PIN — no password needed')}
            </p>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {!supported ? (
          <p className="text-bambu-gray text-sm">
            {t('settings.passkeys.unsupported', 'This browser does not support passkeys, or the page is not served over HTTPS.')}
          </p>
        ) : (
          <div className="space-y-4">
            {credentials && credentials.length > 0 && (
              <div className="space-y-2">
                {credentials.map((cred: WebAuthnCredentialInfo) => (
                  <div
                    key={cred.id}
                    className="flex items-center justify-between bg-bambu-dark-secondary rounded-lg px-4 py-3"
                  >
                    <div>
                      <p className="text-white text-sm font-medium">
                        {cred.device_name || t('settings.passkeys.unnamed', 'Unnamed passkey')}
                      </p>
                      <p className="text-bambu-gray text-xs">
                        {t('settings.passkeys.addedOn', 'Added')} {formatDate(cred.created_at)}
                        {' · '}
                        {t('settings.passkeys.lastUsed', 'Last used')} {formatDate(cred.last_used_at)}
                      </p>
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => deleteMutation.mutate(cred.id)}
                      disabled={deleteMutation.isPending}
                    >
                      <Trash2 className="w-4 h-4 text-red-400" />
                    </Button>
                  </div>
                ))}
              </div>
            )}

            {showAdd ? (
              <div className="space-y-3">
                <input
                  type="text"
                  value={deviceName}
                  onChange={(e) => setDeviceName(e.target.value)}
                  maxLength={150}
                  placeholder={t('settings.passkeys.namePlaceholder', 'Name this device (e.g. Galaxy S26 Ultra)')}
                  className="w-full px-4 py-3 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white placeholder-bambu-gray focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors"
                />
                <div className="flex gap-3">
                  <Button variant="secondary" className="flex-1" onClick={() => setShowAdd(false)}>
                    {t('common.cancel')}
                  </Button>
                  <Button
                    className="flex-1"
                    disabled={registerMutation.isPending}
                    onClick={() => registerMutation.mutate()}
                  >
                    {registerMutation.isPending
                      ? t('settings.passkeys.waiting', 'Follow the prompt…')
                      : t('settings.passkeys.create', 'Create passkey')}
                  </Button>
                </div>
              </div>
            ) : (
              <Button variant="secondary" size="sm" onClick={() => setShowAdd(true)} className="flex items-center gap-2">
                <Plus className="w-4 h-4" />
                {t('settings.passkeys.add', 'Add passkey')}
              </Button>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
