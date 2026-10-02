import { Volume2, VolumeOff } from 'lucide-react'
import { useAuth } from '../context/auth'
import { api } from '../lib/api'
import { Button } from './ui/button'

export function SoundToggle() {
  const { user, updateUser } = useAuth()

  if (!user) return null

  const toggle = () => {
    const prev = user
    const next = !user.soundEnabled
    updateUser({ ...prev, soundEnabled: next })
    void api.settings.update({ soundEnabled: next })
      .then(updateUser)
      .catch(() => updateUser(prev))
  }

  return (
    <Button
      variant="ghost"
      size="icon-sm"
      onClick={toggle}
      aria-label={user.soundEnabled ? 'Mute board sounds' : 'Unmute board sounds'}
    >
      {user.soundEnabled ? <Volume2 className="h-4 w-4" /> : <VolumeOff className="h-4 w-4" />}
    </Button>
  )
}
