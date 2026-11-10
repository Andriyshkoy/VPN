export function planGuidance(code: string) {
  return code === 'basic'
    ? 'Для простых задач, например Instagram, на одном устройстве.'
    : 'Рекомендуем для нескольких устройств и запасного способа подключения.'
}

export const protocolFallbackExplanation = 'Два протокола — два варианта подключения. Если один профиль перестал подключаться в вашей сети, выберите второй в Hiddify вручную.'
