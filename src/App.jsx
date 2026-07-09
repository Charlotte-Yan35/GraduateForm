import { useEffect, useState, Component } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { useFormStore } from './store/formStore'
import { loadDraft, submitForm } from './lib/supabase'
import { useAutoSave } from './hooks/useAutoSave'
import { validateAllSteps } from './lib/validation'
import LoginGate from './components/LoginGate'
import ProgressBar from './components/ProgressBar'
import AutoSaveIndicator from './components/AutoSaveIndicator'
import StepNavigation from './components/StepNavigation'
import Step1 from './pages/Step1_Basic'
import Step2 from './pages/Step2_Background'
import Step3 from './pages/Step3_Results'
import Step4 from './pages/Step4_Experience'
import Step5 from './pages/Step5_Agency'
import Step6 from './pages/Step6_Essay'
import ThankYou from './pages/ThankYou'
import AdminPage from './pages/AdminPage'

const STEPS = [Step1, Step2, Step3, Step4, Step5, Step6]

class ErrorBoundary extends Component {
  state = { error: null }
  static getDerivedStateFromError(error) { return { error } }
  render() {
    if (this.state.error) {
      return (
        <div className="min-h-screen bg-gray-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-lg shadow p-6 max-w-md">
            <h2 className="text-lg font-bold text-red-600 mb-2">出错了</h2>
            <p className="text-sm text-gray-600 mb-4">{this.state.error.message}</p>
            <button onClick={() => this.setState({ error: null })}
              className="px-4 py-2 bg-blue-600 text-white rounded text-sm">重试</button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

function AppInner() {
  const userId = useFormStore((s) => s.userId)
  const submitted = useFormStore((s) => s.submitted)
  const currentStep = useFormStore((s) => s.currentStep)

  const [loading, setLoading] = useState(true)
  const [submitError, setSubmitError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useAutoSave()

  // Always start from login page
  useEffect(() => {
    localStorage.removeItem('fybk_user_id')
    localStorage.removeItem('fybk_identifier')
    localStorage.removeItem('fybk_step')
    setLoading(false)
  }, [])

  const handleSubmit = async () => {
    const state = useFormStore.getState()
    const { valid, firstFailingStep } = validateAllSteps(state.formData)
    if (!valid) {
      state.setStep(firstFailingStep)
      localStorage.setItem('fybk_step', String(firstFailingStep))
      window.scrollTo(0, 0)
      setTimeout(() => window.dispatchEvent(new CustomEvent('validate-step')), 100)
      return
    }
    setSubmitting(true)
    setSubmitError('')
    try {
      const { error } = await submitForm(state.submissionId, state.formData)
      if (error) throw error
      useFormStore.getState().setSubmitted(true)
    } catch {
      setSubmitError('提交失败，请检查网络后重试')
    } finally {
      setSubmitting(false)
    }
  }

  if (loading) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <p className="text-gray-400">加载中…</p>
      </div>
    )
  }

  if (!userId) return <LoginGate />
  if (submitted) return <ThankYou />

  const StepComponent = STEPS[currentStep - 1]

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-2xl mx-auto px-4 py-8">
        <h1 className="text-xl font-bold text-gray-900 mb-1">飞跃手册问卷 · 26Fall</h1>
        <AutoSaveIndicator />
        <ProgressBar />
        <div className="bg-white rounded-2xl shadow-sm p-6">
          <StepComponent />
          {submitError && (
            <p className="text-red-500 text-sm mt-4">{submitError}</p>
          )}
          <StepNavigation onSubmit={handleSubmit} />
          {submitting && (
            <p className="text-center text-sm text-gray-400 mt-2">提交中…</p>
          )}
        </div>
      </div>
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <ErrorBoundary>
        <Routes>
          <Route path="/" element={<AppInner />} />
          <Route path="/admin" element={<AdminPage />} />
        </Routes>
      </ErrorBoundary>
    </BrowserRouter>
  )
}
