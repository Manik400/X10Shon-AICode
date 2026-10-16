import { HashRouter, Route, Routes } from 'react-router-dom';
import Layout from './components/Layout';
import Activity from './pages/Activity';
import Applications from './pages/Applications';
import Jobs from './pages/Jobs';
import Overview from './pages/Overview';
import Profile from './pages/Profile';
import Settings from './pages/Settings';

export default function App() {
  return (
    <HashRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Overview />} />
          <Route path="jobs" element={<Jobs />} />
          <Route path="applications" element={<Applications />} />
          <Route path="profile" element={<Profile />} />
          <Route path="settings" element={<Settings />} />
          <Route path="activity" element={<Activity />} />
        </Route>
      </Routes>
    </HashRouter>
  );
}
