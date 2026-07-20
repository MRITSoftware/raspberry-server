-- Publica uma nova versao stable do agente Raspberry.
-- Troque os valores COMMIT_* e notes antes de executar.

update public.tuya_server_releases
set active = false
where channel = 'stable'
  and active = true;

insert into public.tuya_server_releases (
  channel,
  app_version,
  commit_sha,
  commit_short,
  branch,
  notes,
  active
)
values (
  'stable',
  '1.0-PI-wifi-rescue',
  'COMMIT_COMPLETO_AQUI',
  'COMMIT_CURTO_AQUI',
  'main',
  'Descricao da atualizacao',
  true
);
