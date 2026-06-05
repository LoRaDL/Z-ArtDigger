import React, { useState, useEffect } from 'react'

const API_BASE = "http://localhost:8000/api"

function App() {
  const [stats, setStats] = useState({
    authors_count: 0, islands_count: 0, total_tweets: 0,
    status: 'Stopped', api_queue: 0, l3_queue: 0, task_pool_size: 0,
    active_workers: {}, last_vision: null,
    api_limit: {},
    classification_hits: {}
  })
  const [authors, setAuthors] = useState([])
  const [timelineData, setTimelineData] = useState({})
  const [selectedWorker, setSelectedWorker] = useState(null)
  const [workerLogs, setWorkerLogs] = useState([])
  const [tasks, setTasks] = useState([])
  const [archives, setArchives] = useState([])
  const [archivePage, setArchivePage] = useState(1)
  const [archiveTotal, setArchiveTotal] = useState(0)
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [currentTab, setCurrentTab] = useState('Overview') // 'Overview', 'Pools', 'Logs', 'Archives'

  // ... (existing useEffects for fetchData, fetchIslands, fetchWorkerLogs) ...

  // SSE: 实时监听全局统计和大模型分类结果
  useEffect(() => {
    const eventSource = new EventSource(`${API_BASE}/stream/stats`);
    eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data);
      setStats(data);
    };
    eventSource.onerror = (e) => {
      console.error("SSE Error", e);
    };
    return () => eventSource.close();
  }, []);

  // 轮询其他相对较重/不那么紧急的数据 (任务池、作者、时间线)
  useEffect(() => {
    const fetchOtherData = async () => {
      try {
        const authRes = await fetch(`${API_BASE}/authors`).then(res => res.json());
        setAuthors(authRes);

        const tasksRes = await fetch(`${API_BASE}/tasks`).then(res => res.json());
        setTasks(tasksRes);

        const tlRes = await fetch(`${API_BASE}/timeline_data`).then(res => res.json());
        setTimelineData(tlRes);
      } catch (e) {
        console.error("API Error", e);
      }
    };
    fetchOtherData();
    const timer = setInterval(fetchOtherData, 3000);
    return () => clearInterval(timer);
  }, []);

  // 针对分页档案面板独立轮询
  useEffect(() => {
    const fetchArchives = async () => {
      try {
        const arcRes = await fetch(`${API_BASE}/archives?page=${archivePage}&limit=24`).then(res => res.json())
        if (arcRes.items) {
          setArchives(arcRes.items)
          setArchiveTotal(arcRes.total)
        }
      } catch (e) { console.error(e) }
    }
    fetchArchives()
    const timer = setInterval(fetchArchives, 3000)
    return () => clearInterval(timer)
  }, [archivePage])

  // 计算全局时间线边界 (基于 Snowflake ID 也是基于时间的)
  const SNOWFLAKE_EPOCH = 1288834974657n;
  const dateToSnowflake = (dateStr) => {
    if (!dateStr) return null;
    const time = new Date(dateStr).getTime();
    if (isNaN(time)) return null;
    return (BigInt(time) - SNOWFLAKE_EPOCH) << 22n;
  };

  const manualMin = dateToSnowflake(startDate);
  // 对于 endDate，加上一整天的时间量保证查看到该日结束 (24h)
  const manualMax = dateToSnowflake(endDate) ? dateToSnowflake(endDate) + (24n * 3600n * 1000n << 22n) : null;

  let dataMinId = null;
  let dataMaxId = null;

  Object.values(timelineData).forEach(authorData => {
    authorData.islands.forEach(isl => {
      const min = BigInt(isl.min_id);
      const max = BigInt(isl.max_id);
      if (dataMinId === null || min < dataMinId) dataMinId = min;
      if (dataMaxId === null || max > dataMaxId) dataMaxId = max;
    });
    authorData.tweets.forEach(t => {
      const id = BigInt(t.id);
      if (dataMinId === null || id < dataMinId) dataMinId = id;
      if (dataMaxId === null || id > dataMaxId) dataMaxId = id;
    });
  });

  const minId = manualMin !== null ? manualMin : dataMinId;
  const maxId = manualMax !== null ? manualMax : dataMaxId;

  const span = maxId !== null && minId !== null && maxId > minId ? Number(maxId - minId) : 1;
  const paddedSpan = span > 0 ? span * 1.05 : 10000;
  const startId = minId !== null ? minId - BigInt(Math.floor(span * 0.025)) : 0n;

  const getPercent = (idStr) => {
    if (paddedSpan === 0) return 50;
    const val = Number(BigInt(idStr) - startId);
    return Math.max(-5, Math.min(105, (val / paddedSpan) * 100)); // 允许稍微溢出边界但不穿透太多
  };


  // 选中 Worker 后轮询日志
  useEffect(() => {
    if (!selectedWorker) return
    const fetchLogs = async () => {
      try {
        const res = await fetch(`${API_BASE}/logs/${selectedWorker}`).then(res => res.json())
        setWorkerLogs(res)
      } catch (e) { console.error(e) }
    }
    fetchLogs()
    const timer = setInterval(fetchLogs, 2000)
    return () => clearInterval(timer)
  }, [selectedWorker])

  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])

  const formatCountdown = (resetTs) => {
    if (!resetTs) return '--:--'
    const diff = Math.max(0, Math.floor(resetTs - now / 1000))
    const m = Math.floor(diff / 60)
    const s = diff % 60
    return `${m}:${s.toString().padStart(2, '0')}`
  }

  const handleDelete = async (filename) => {
    try {
      const res = await fetch(`${API_BASE}/archives/${filename}`, { method: 'DELETE' }).then(res => res.json())
      if (res.status === 'success') {
        setArchives(prev => prev.filter(a => a.filename !== filename))
        setArchiveTotal(prev => prev - 1)
      }
    } catch (e) {
      console.error("Delete failed", e)
    }
  }

  return (
    <div className="flex min-h-screen bg-background text-gray-100 font-sans">
      {/* Sidebar */}
      <aside className="w-64 border-r border-gray-800 flex flex-col fixed h-full bg-surface shadow-xl z-10">
        <div className="p-8 pb-4">
          <h1 className="text-xl font-black tracking-tight flex items-center">
            Z-ART<span className="text-primary ml-1">DIGGER</span>
          </h1>
          <p className="text-[10px] text-gray-600 font-bold uppercase tracking-widest mt-1">Crawler Control Hub</p>
        </div>

        <nav className="flex-1 mt-10 px-4 space-y-2">
          <SidebarButton
            active={currentTab === 'Overview'}
            onClick={() => setCurrentTab('Overview')}
            label="Overview"
            icon="M4 5a1 1 0 011-1h14a1 1 0 011 1v2a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM4 13a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H5a1 1 0 01-1-1v-6zM16 13a1 1 0 011-1h2a1 1 0 011 1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-6z"
          />
          <SidebarButton
            active={currentTab === 'Pools'}
            onClick={() => setCurrentTab('Pools')}
            label="Pools & Queues"
            icon="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"
          />
          <SidebarButton
            active={currentTab === 'Logs'}
            onClick={() => setCurrentTab('Logs')}
            label="Worker Logs"
            icon="M4 6h16M4 12h16m-7 6h7"
          />
          <SidebarButton
            active={currentTab === 'Archives'}
            onClick={() => setCurrentTab('Archives')}
            label="Local Archives"
            icon="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4"
          />
        </nav>

        <div className="p-6 border-t border-gray-800">
          <div className="flex items-center space-x-3">
            <div className={`w-2 h-2 rounded-full ${stats.status === 'Running' ? 'bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)]' : 'bg-gray-500'}`} />
            <span className="text-xs font-bold uppercase tracking-wider text-gray-400">{stats.status}</span>
          </div>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 ml-64 p-12 overflow-y-auto">
        {currentTab === 'Overview' && (
          <div className="max-w-7xl mx-auto space-y-12 animate-in fade-in duration-500">
            {/* Stats Grid */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
              <StatCard label="Total Authors" value={stats.authors_count} />
              <StatCard label="Timeline Islands" value={stats.islands_count} />
              <StatCard label="Archived Tweets" value={stats.total_tweets} />
            </div>

            {/* Classification Hits Dashboard */}
            <div className="bg-surface rounded-2xl border border-gray-800 p-8 shadow-sm">
              <div className="flex flex-col md:flex-row justify-between items-start md:items-center mb-6">
                <div>
                  <h2 className="text-xs font-bold text-gray-500 uppercase tracking-widest">Multi-Layer Classification Hits</h2>
                  <p className="text-[10px] text-gray-600 mt-1">Real-time counts of tweet classification decisions from L0 cache to L3 Vision LLM</p>
                </div>
                <div className="text-[10px] text-gray-400 font-bold uppercase bg-gray-900/50 px-3 py-1.5 rounded-lg border border-gray-800 mt-2 md:mt-0">
                  Total Classifications: {
                    Object.values(stats.classification_hits || {}).reduce((a, b) => a + b, 0)
                  }
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
                {/* L0 Cache */}
                <div className="bg-black/30 rounded-xl p-5 border border-gray-800/80 flex flex-col justify-between relative group hover:border-gray-700 transition-colors">
                  <div>
                    <div className="flex justify-between items-start">
                      <span className="px-2 py-0.5 rounded text-[9px] font-black uppercase bg-blue-500/10 text-blue-400 border border-blue-500/20">L0 Layer</span>
                      <span className="text-[10px] text-gray-500 font-bold font-mono">CACHE</span>
                    </div>
                    <h3 className="text-sm font-bold text-gray-200 mt-3 mb-1">Local Decision Cache</h3>
                    <p className="text-[10px] text-gray-600 leading-tight">Short-circuits classification using previously stored results</p>
                  </div>
                  
                  <div className="mt-6 space-y-2 border-t border-gray-800/50 pt-4">
                    <div className="flex justify-between items-center">
                      <span className="text-[10px] text-gray-500 font-bold uppercase tracking-wider flex items-center">
                        <span className="w-1.5 h-1.5 rounded-full bg-green-500 mr-2 animate-pulse" /> Related
                      </span>
                      <span className="text-sm font-mono font-bold text-green-400">{stats.classification_hits?.L0_related || 0}</span>
                    </div>
                    <div className="flex justify-between items-center">
                      <span className="text-[10px] text-gray-500 font-bold uppercase tracking-wider flex items-center">
                        <span className="w-1.5 h-1.5 rounded-full bg-red-500 mr-2" /> Unrelated
                      </span>
                      <span className="text-sm font-mono font-bold text-red-400">{stats.classification_hits?.L0_unrelated || 0}</span>
                    </div>
                  </div>
                </div>

                {/* L1 URL Match */}
                <div className="bg-black/30 rounded-xl p-5 border border-gray-800/80 flex flex-col justify-between relative group hover:border-gray-700 transition-colors">
                  <div>
                    <div className="flex justify-between items-start">
                      <span className="px-2 py-0.5 rounded text-[9px] font-black uppercase bg-orange-500/10 text-orange-400 border border-orange-500/20">L1 Layer</span>
                      <span className="text-[10px] text-gray-500 font-bold font-mono">URL MATCH</span>
                    </div>
                    <h3 className="text-sm font-bold text-gray-200 mt-3 mb-1">GalleryDB URL Match</h3>
                    <p className="text-[10px] text-gray-600 leading-tight">Matches tweet image URLs directly against existing artworks</p>
                  </div>
                  
                  <div className="mt-6 space-y-2 border-t border-gray-800/50 pt-4">
                    <div className="flex justify-between items-center">
                      <span className="text-[10px] text-gray-500 font-bold uppercase tracking-wider flex items-center">
                        <span className="w-1.5 h-1.5 rounded-full bg-green-500 mr-2 animate-pulse" /> Related
                      </span>
                      <span className="text-sm font-mono font-bold text-green-400">{stats.classification_hits?.L1_related || 0}</span>
                    </div>
                    <div className="flex justify-between items-center text-gray-700">
                      <span className="text-[10px] text-gray-600 font-bold uppercase tracking-wider flex items-center">
                        <span className="w-1.5 h-1.5 rounded-full bg-gray-800 mr-2" /> Unrelated
                      </span>
                      <span className="text-xs font-mono italic">fall through</span>
                    </div>
                  </div>
                </div>

                {/* L2 pHash Match */}
                <div className="bg-black/30 rounded-xl p-5 border border-gray-800/80 flex flex-col justify-between relative group hover:border-gray-700 transition-colors">
                  <div>
                    <div className="flex justify-between items-start">
                      <span className="px-2 py-0.5 rounded text-[9px] font-black uppercase bg-purple-500/10 text-purple-400 border border-purple-500/20">L2 Layer</span>
                      <span className="text-[10px] text-gray-500 font-bold font-mono">PHASH</span>
                    </div>
                    <h3 className="text-sm font-bold text-gray-200 mt-3 mb-1">Perceptual Hashing</h3>
                    <p className="text-[10px] text-gray-600 leading-tight">Fuzzy visual comparison against downloaded artwork gallery hashes</p>
                  </div>
                  
                  <div className="mt-6 space-y-2 border-t border-gray-800/50 pt-4">
                    <div className="flex justify-between items-center">
                      <span className="text-[10px] text-gray-500 font-bold uppercase tracking-wider flex items-center">
                        <span className="w-1.5 h-1.5 rounded-full bg-green-500 mr-2 animate-pulse" /> Related
                      </span>
                      <span className="text-sm font-mono font-bold text-green-400">{stats.classification_hits?.L2_related || 0}</span>
                    </div>
                    <div className="flex justify-between items-center text-gray-700">
                      <span className="text-[10px] text-gray-600 font-bold uppercase tracking-wider flex items-center">
                        <span className="w-1.5 h-1.5 rounded-full bg-gray-800 mr-2" /> Unrelated
                      </span>
                      <span className="text-xs font-mono italic">fall through</span>
                    </div>
                  </div>
                </div>

                {/* L3 Vision LLM */}
                <div className="bg-black/30 rounded-xl p-5 border border-gray-800/80 flex flex-col justify-between relative group hover:border-gray-700 transition-colors">
                  <div>
                    <div className="flex justify-between items-start">
                      <span className="px-2 py-0.5 rounded text-[9px] font-black uppercase bg-pink-500/10 text-pink-400 border border-pink-500/20">L3 Layer</span>
                      <span className="text-[10px] text-gray-500 font-bold font-mono">VISION LLM</span>
                    </div>
                    <h3 className="text-sm font-bold text-gray-200 mt-3 mb-1">Semantic Vision AI</h3>
                    <p className="text-[10px] text-gray-600 leading-tight">Zero-shot multi-modal evaluation of illustration content & style</p>
                  </div>
                  
                  <div className="mt-6 space-y-2 border-t border-gray-800/50 pt-4">
                    <div className="flex justify-between items-center">
                      <span className="text-[10px] text-gray-500 font-bold uppercase tracking-wider flex items-center">
                        <span className="w-1.5 h-1.5 rounded-full bg-green-500 mr-2 animate-pulse" /> Related
                      </span>
                      <span className="text-sm font-mono font-bold text-green-400">{stats.classification_hits?.L3_related || 0}</span>
                    </div>
                    <div className="flex justify-between items-center">
                      <span className="text-[10px] text-gray-500 font-bold uppercase tracking-wider flex items-center">
                        <span className="w-1.5 h-1.5 rounded-full bg-red-500 mr-2" /> Unrelated
                      </span>
                      <span className="text-sm font-mono font-bold text-red-400">{stats.classification_hits?.L3_unrelated || 0}</span>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div className="bg-surface rounded-2xl border border-gray-800 p-8 min-h-[400px]">
              <div className="flex flex-col md:flex-row md:items-center justify-between mb-8 space-y-4 md:space-y-0">
                <h2 className="text-xs font-bold text-gray-500 uppercase tracking-widest">Global Timeline Synchronization</h2>
                <div className="flex items-center space-x-3 bg-gray-900/50 p-2 rounded-xl border border-gray-800 shadow-inner">
                  <span className="text-[10px] uppercase font-bold text-gray-500 ml-2">Range</span>
                  <input type="date" value={startDate} onChange={e => setStartDate(e.target.value)} className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-gray-300 outline-none focus:border-primary transition-colors cursor-pointer" />
                  <span className="text-gray-600 text-xs">—</span>
                  <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)} className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-gray-300 outline-none focus:border-primary transition-colors cursor-pointer" />
                  {(startDate || endDate) && (
                    <button onClick={() => { setStartDate(''); setEndDate(''); }} className="text-[10px] px-2 text-gray-400 hover:text-white transition-colors font-bold uppercase tracking-wider">
                      Reset
                    </button>
                  )}
                </div>
              </div>
              {Object.keys(timelineData).length > 0 ? (
                <div className="space-y-4">
                  {Object.entries(timelineData).map(([authorName, authorData]) => (
                    <div key={authorName} className="flex flex-col md:flex-row md:items-start space-y-4 md:space-y-0 md:space-x-6">
                      {/* Author Label */}
                      <div className="md:w-36 flex-shrink-0 pt-1">
                        <div className="font-bold text-gray-200 truncate">@{authorName}</div>
                        <div className="text-[10px] text-gray-500 mt-1 uppercase tracking-wider">
                          {authorData.islands.reduce((acc, isl) => acc + isl.image_count, 0)} PICS
                        </div>
                      </div>

                      {/* Sub-Timeline Track */}
                      <div className="flex-1 relative h-6 bg-gray-900/50 rounded-lg border border-gray-800 shadow-inner group overflow-hidden mt-1">
                        {/* Islands Boundaries */}
                        {authorData.islands.map((isl, i) => {
                          const leftP = getPercent(isl.min_id);
                          const rightP = getPercent(isl.max_id);
                          const widthP = Math.max(rightP - leftP, 0.2); // 最小可见宽度
                          return (
                            <div
                              key={`isl-${i}`}
                              className="absolute top-0 h-full bg-primary/20 border-x border-primary/40 transition-all group-hover:bg-primary/30"
                              style={{ left: `${leftP}%`, width: `${widthP}%` }}
                            />
                          );
                        })}


                        {/* Tweets Points */}
                        {authorData.tweets.map((t, i) => {
                          const leftP = getPercent(t.id);
                          return (
                            <div
                              key={`t-${i}`}
                              className={`absolute top-1/2 w-0.5 h-4 -mt-2 rounded-full ${t.has_image ? 'bg-green-400 opacity-80 z-10' : 'bg-gray-600 opacity-40'} transition-all hover:scale-150 hover:z-20`}
                              style={{ left: `${leftP}%` }}
                              title={`ID: ${t.id}${t.has_image ? ' (Media)' : ''}`}
                            />
                          );
                        })}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-gray-600 italic text-center py-12 border border-dashed border-gray-800 rounded-xl">
                  Waiting for synchronized timeline data...
                </div>
              )}
            </div>
          </div>
        )}

        {currentTab === 'Pools' && (
          <div className="max-w-6xl mx-auto space-y-8 animate-in slide-in-from-right-8 duration-500">
            {/* Pool Queues */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
              <PoolCard
                label="API Traffic Pool"
                value={stats.api_queue}
                unit="Requests"
                color="text-orange-400"
                desc="Pending X API requests in serialization queue"
              />
              <PoolCard
                label="L3 Vision Pool"
                value={stats.l3_queue}
                unit="Images"
                color="text-purple-400"
                desc="Images waiting for VisionLLM semantic check"
              />
              <PoolCard
                label="Task Discovery Pool"
                value={stats.task_pool_size}
                unit="Tasks"
                color="text-blue-400"
                desc="Scan tasks discovered and waiting in Redis queue"
              />
            </div>

            {/* API Rate Limit Monitor */}
            <div className="bg-surface border border-gray-800 rounded-2xl p-8 shadow-sm overflow-hidden relative group">
              <div className="flex justify-between items-center mb-6">
                <div>
                  <h2 className="text-xs font-bold text-gray-500 uppercase tracking-widest">X API Resource Quota</h2>
                  <p className="text-[10px] text-gray-600 mt-1">Real-time tracking of endpoint rate limits and reset windows per active account</p>
                </div>
              </div>

              {Object.keys(stats.api_limit || {}).length > 0 ? (
                <div className="space-y-6">
                  {Object.entries(stats.api_limit).map(([cookieName, limitData]) => {
                    const remaining = limitData.remaining ?? 180;
                    const limit = limitData.limit ?? 180;
                    const reset = limitData.reset ?? 0;
                    const endpoint = limitData.endpoint ?? 'N/A';
                    
                    const isLow = remaining < 10;
                    const percent = Math.min(100, (remaining / limit) * 100);

                    return (
                      <div key={cookieName} className="grid grid-cols-1 md:grid-cols-12 gap-6 items-center bg-black/20 p-5 rounded-xl border border-gray-800/80 hover:border-gray-700 transition-colors">
                        {/* Cookie/Account Label */}
                        <div className="md:col-span-3 flex flex-col">
                          <span className="text-[10px] text-gray-500 font-black uppercase tracking-widest">Account</span>
                          <span className="text-sm font-bold text-gray-200 truncate mt-1 flex items-center">
                            <span className={`w-1.5 h-1.5 rounded-full mr-2 ${isLow ? 'bg-red-500 animate-pulse' : 'bg-green-500'}`} />
                            {cookieName.replace(".txt", "")}
                          </span>
                          <span className="text-[9px] font-mono text-gray-600 mt-0.5">Endpoint: {endpoint}</span>
                        </div>

                        {/* Capacity Health */}
                        <div className="md:col-span-6 flex flex-col">
                          <div className="flex justify-between items-end mb-1.5">
                            <span className="text-[9px] text-gray-500 font-bold uppercase tracking-widest">Capacity Health</span>
                            <span className="text-xs font-mono text-gray-400">
                              <span className="text-primary font-bold">{remaining}</span>
                              <span className="mx-1 opacity-30">/</span>
                              <span className="opacity-60">{limit}</span>
                            </span>
                          </div>
                          <div className="w-full h-2.5 bg-gray-900 rounded-full overflow-hidden border border-gray-800 p-0.5">
                            <div
                              className={`h-full rounded-full transition-all duration-1000 ${isLow ? 'bg-red-500 shadow-[0_0_8px_rgba(239,68,68,0.5)]' : 'bg-primary shadow-[0_0_8px_rgba(59,130,246,0.3)]'}`}
                              style={{ width: `${percent}%` }}
                            />
                          </div>
                        </div>

                        {/* Resets In */}
                        <div className="md:col-span-3 text-left md:text-right flex flex-col md:items-end justify-center">
                          <span className="text-[9px] text-gray-500 font-bold uppercase tracking-widest mb-1">Resets In</span>
                          <span className="text-lg font-black text-gray-200 font-mono tracking-tight">
                            {formatCountdown(reset)}
                          </span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="text-gray-600 italic text-center py-8 border border-dashed border-gray-800 rounded-xl text-xs">
                  Awaiting rate limit reports from active crawler sessions...
                </div>
              )}
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
              {/* Task Queue List */}
              <div className="lg:col-span-2 space-y-6">
                <div className="bg-surface border border-gray-800 rounded-2xl p-8 shadow-sm">
                  <h2 className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-6">Tasks in Priority Pool</h2>
                  <div className="bg-black/40 rounded-xl p-4 h-[500px] overflow-y-auto border border-gray-800/50 scrollbar-hide space-y-2">
                    {tasks.map((t, i) => (
                      <div key={i} className="flex justify-between items-center text-xs p-3 bg-gray-900/40 rounded-lg border border-gray-800">
                        <div className="font-mono text-gray-300 truncate pr-4">{t.task}</div>
                        <div className="text-blue-400 font-bold whitespace-nowrap">Score: {t.score.toFixed(3)}</div>
                      </div>
                    ))}
                    {tasks.length === 0 && <div className="text-gray-600 text-sm italic py-4 text-center">No tasks in queue.</div>}
                  </div>
                </div>
              </div>

              {/* Latest Vision Result */}
              <div className="lg:col-span-1 space-y-6">
                <div className="bg-surface border border-gray-800 rounded-2xl p-8 sticky top-12 shadow-sm overflow-hidden">
                  <h2 className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-6">Vision LLM Daily</h2>

                  {stats.last_vision ? (
                    <div className="space-y-6">
                      <div className="aspect-square bg-gray-900 rounded-xl overflow-hidden border border-gray-800 relative">
                        <img
                          src={`http://localhost:8000/images/${stats.last_vision.filename}`}
                          alt="latest-vision"
                          className="w-full h-full object-cover"
                        />
                        <div className="absolute bottom-0 left-0 right-0 p-4 bg-gradient-to-t from-black/80 to-transparent">
                          <span className={`px-2 py-1 rounded text-[9px] font-black uppercase ${stats.last_vision.result === 'related' ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400'
                            }`}>
                            {stats.last_vision.result}
                          </span>
                        </div>
                      </div>
                      <div className="space-y-1">
                        <p className="text-[10px] text-gray-600 font-bold uppercase tracking-widest">Metadata</p>
                        <p className="text-xs font-mono text-gray-400 truncate">{stats.last_vision.filename}</p>
                        <p className="text-[10px] text-gray-700">{new Date(stats.last_vision.timestamp * 1000).toLocaleString()}</p>
                      </div>
                    </div>
                  ) : (
                    <div className="h-64 flex flex-col items-center justify-center border-2 border-dashed border-gray-800/50 rounded-xl text-gray-700">
                      <p className="text-xs italic">Awaiting first classification...</p>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}

        {currentTab === 'Logs' && (
          <div className="max-w-6xl mx-auto space-y-8 animate-in slide-in-from-bottom-8 duration-500">
            <div className="bg-surface border border-gray-800 rounded-2xl p-8 shadow-sm">
              <h2 className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-6">Active Workers Context</h2>
              <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
                {Object.entries(stats.active_workers || {}).map(([id, info]) => (
                  <button
                    key={id}
                    onClick={() => setSelectedWorker(id)}
                    className={`p-4 rounded-xl border transition-all text-left flex flex-col justify-between h-32 ${selectedWorker === id
                      ? 'bg-primary/20 border-primary ring-1 ring-primary'
                      : info.detail === 'Idle'
                        ? 'bg-gray-900/10 border-gray-800/30 opacity-40'
                        : 'bg-gray-900/40 border-gray-800 hover:border-gray-600'
                      }`}
                  >
                    <div className="flex justify-between items-start">
                      <span className={`px-2 py-1 rounded-md text-[10px] font-black uppercase ${info.detail === 'Idle' ? 'bg-gray-700/50 text-gray-500' :
                        info.type === 'Crawler' ? 'bg-blue-500/20 text-blue-400' :
                          info.type === 'Filler' ? 'bg-green-500/20 text-green-400' :
                            info.type === 'Vision' ? 'bg-purple-500/20 text-purple-400' :
                              info.type === 'API' ? 'bg-orange-500/20 text-orange-400' :
                                'bg-gray-500/20 text-gray-400'
                        }`}>
                        {info.type}
                      </span>
                      {selectedWorker === id && <span className="w-1.5 h-1.5 bg-primary rounded-full animate-ping" />}
                    </div>
                    <div>
                      <div className={`text-sm font-bold truncate ${info.detail === 'Idle' ? 'text-gray-600' : 'text-gray-100'}`}>
                        {info.detail === 'Idle' ? 'READY' : `@${info.author}`}
                      </div>
                      <div className="text-[10px] text-gray-500 truncate mt-1 font-mono opacity-80 uppercase tracking-tighter">
                        {info.detail}
                      </div>
                    </div>
                  </button>
                ))}
              </div>

              {selectedWorker && (
                <div className="mt-8 pt-8 border-t border-gray-800 animate-in slide-in-from-bottom-4 duration-300">
                  <div className="flex justify-between items-center mb-4">
                    <h3 className="text-[10px] font-black text-primary uppercase tracking-widest">Worker Trace • {selectedWorker.slice(-6)}</h3>
                    <button onClick={() => setSelectedWorker(null)} className="text-[10px] text-gray-600 hover:text-white font-bold">DISMISS</button>
                  </div>
                  <div className="bg-black/40 rounded-xl p-4 h-[500px] overflow-y-auto font-mono text-[11px] border border-gray-800/50 scrollbar-hide">
                    {workerLogs.length > 0 ? (
                      workerLogs.map((log, i) => (
                        <div key={i} className="mb-1 text-gray-500 flex">
                          <span className="text-gray-700 shrink-0 w-8">{(i + 1).toString().padStart(2, '0')}</span>
                          <span className="break-all">{log}</span>
                        </div>
                      ))
                    ) : (
                      <div className="text-gray-800 italic">No historical traces for this thread ID.</div>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {currentTab === 'Archives' && (
          <div className="max-w-6xl mx-auto space-y-8 animate-in slide-in-from-bottom-8 duration-500">
            <div className="bg-surface border border-gray-800 rounded-2xl p-8 shadow-sm">
              <h2 className="text-xs font-bold text-gray-500 uppercase tracking-widest mb-6">Local Archives Inspector</h2>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
                {archives.map((arc, i) => (
                  <div key={i} className="bg-gray-900/40 border border-gray-800 rounded-xl overflow-hidden shadow-sm hover:border-gray-600 transition-colors">
                    {arc.image_name ? (
                      <div className="aspect-square bg-black border-b border-gray-800 relative group">
                        <img
                          src={`http://localhost:8000/images/${arc.image_name}`}
                          alt="Archived Media"
                          className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
                        />
                        <button
                          onClick={(e) => { e.stopPropagation(); handleDelete(arc.filename); }}
                          className="absolute top-2 right-2 p-1.5 bg-red-500/80 hover:bg-red-600 text-white rounded-lg opacity-0 group-hover:opacity-100 transition-opacity z-20 shadow-lg"
                          title="Move to Deleted"
                        >
                          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" d="M6 18L18 6M6 6l12 12" />
                          </svg>
                        </button>
                      </div>
                    ) : (
                      <div className="aspect-[2/1] bg-gray-900 border-b border-gray-800 flex items-center justify-center p-4 relative group">
                        <span className="text-[10px] text-gray-600 italic tracking-widest uppercase text-center">No Media Cache</span>
                        <button
                          onClick={(e) => { e.stopPropagation(); handleDelete(arc.filename); }}
                          className="absolute top-2 right-2 p-1.5 bg-red-500/80 hover:bg-red-600 text-white rounded-lg opacity-0 group-hover:opacity-100 transition-opacity z-20 shadow-lg"
                        >
                          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" d="M6 18L18 6M6 6l12 12" />
                          </svg>
                        </button>
                      </div>
                    )}
                    <div className="p-4 space-y-3">
                      <div className="flex justify-between items-start">
                        <span className="text-sm font-bold text-gray-200 truncate pr-2">@{arc.author}</span>
                        <span className="text-[9px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-400 font-mono flex-shrink-0">ID: {arc.tweet_id && arc.tweet_id.toString().slice(-6)}</span>
                      </div>
                      <p className="text-[10px] text-gray-500 truncate">{arc.created_at || 'Unknown Date'}</p>
                      <p className="text-xs text-gray-400 break-words line-clamp-3 leading-relaxed">{arc.content}</p>
                      <p className="text-[9px] text-primary/60 font-mono truncate border-t border-gray-800 pt-3 mt-2" title={arc.filename}>
                        {arc.filename}
                      </p>
                    </div>
                  </div>
                ))}
              </div>

              {/* 分页控制台 */}
              {archiveTotal > 0 && (
                <div className="flex justify-center items-center space-x-6 pt-10 border-t border-gray-800">
                  <button
                    onClick={() => setArchivePage(p => Math.max(1, p - 1))}
                    disabled={archivePage === 1}
                    className="px-6 py-2.5 bg-gray-900 border border-gray-800 hover:bg-gray-800 hover:text-white disabled:opacity-30 disabled:hover:bg-gray-900 rounded-xl text-sm font-bold transition-colors"
                  >
                    Previous
                  </button>
                  <span className="text-[11px] font-mono text-gray-400 tracking-widest uppercase">
                    Page {archivePage} <span className="mx-2 opacity-30">/</span> {Math.ceil(archiveTotal / 24) || 1}
                    <span className="mx-3 opacity-30">|</span>
                    {archiveTotal} Records
                  </span>
                  <button
                    onClick={() => setArchivePage(p => Math.min(Math.ceil(archiveTotal / 24), p + 1))}
                    disabled={archivePage >= Math.ceil(archiveTotal / 24) || archiveTotal === 0}
                    className="px-6 py-2.5 bg-gray-900 border border-gray-800 hover:bg-gray-800 hover:text-white disabled:opacity-30 disabled:hover:bg-gray-900 rounded-xl text-sm font-bold transition-colors"
                  >
                    Next
                  </button>
                </div>
              )}

              {archives.length === 0 && (
                <div className="text-center text-gray-600 py-16 italic border border-dashed border-gray-800 rounded-xl">
                  No local archives found in output directory.
                </div>
              )}
            </div>
          </div>
        )}
      </main>
    </div>
  )
}

function SidebarButton({ active, onClick, label, icon }) {
  return (
    <button
      onClick={onClick}
      className={`w-full flex items-center space-x-3 px-4 py-3 rounded-xl transition-all ${active
        ? 'bg-primary text-white shadow-lg shadow-primary/20'
        : 'text-gray-500 hover:text-white hover:bg-gray-800/50'
        }`}
    >
      <svg className="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d={icon} />
      </svg>
      <span className="text-sm font-bold">{label}</span>
    </button>
  )
}

function PoolCard({ label, value, unit, color, desc }) {
  return (
    <div className="bg-surface p-8 rounded-2xl border border-gray-800 shadow-sm flex flex-col justify-between">
      <div>
        <div className="text-[10px] text-gray-500 font-black uppercase tracking-widest mb-1">{label}</div>
        <p className="text-[10px] text-gray-600 leading-tight mb-4">{desc}</p>
      </div>
      <div className="flex items-baseline space-x-2">
        <span className={`text-4xl font-black ${color}`}>{value}</span>
        <span className="text-xs font-bold text-gray-600 uppercase pt-4">{unit}</span>
      </div>
    </div>
  )
}

function StatCard({ label, value }) {
  return (
    <div className="bg-surface p-8 rounded-2xl border border-gray-800 shadow-sm relative overflow-hidden group">
      <div className="absolute top-0 right-0 w-32 h-32 bg-primary/5 -mr-16 -mt-16 rounded-full group-hover:scale-110 transition-transform duration-700" />
      <div className="text-[10px] text-gray-500 font-black uppercase tracking-widest mb-1 relative z-1">{label}</div>
      <div className="text-4xl font-black relative z-1">{value}</div>
    </div>
  )
}

export default App
