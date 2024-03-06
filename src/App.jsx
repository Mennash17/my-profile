import { useState } from 'react'
import './App.css'
import Tabs from './componants/Tabs'

function App() {
  const [count, setCount] = useState(0)

  return (
    <>
    <Tabs />
    </>
  )
}

export default App
