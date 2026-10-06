import axios from 'axios';
import { ApiError } from '../utils/ApiError.js';
import { pushHistoryItem } from './historyController.js';

// Helper to compute timeline grouping key (e.g. "GET:https://api.example.com/users")
const getTimelineKey = (method, url) => {
  try {
    const u = new URL(url);
    return `${method.toUpperCase()}:${u.origin}${u.pathname}`;
  } catch {
    const base = (url || '').split('?')[0];
    return `${method.toUpperCase()}:${base}`;
  }
};

// Executes an outgoing HTTP request on behalf of the client
const executeProxyRequest = async ({ url, method, headers, params, body }) => {
  if (url.startsWith('file://')) {
    throw new ApiError(403, 'Access to local file protocol is forbidden.');
  }

  const axiosConfig = {
    url,
    method: method.toUpperCase(),
    headers: headers && typeof headers === 'object' ? headers : {},
    validateStatus: () => true, // Treat all HTTP response codes as valid resolves
  };

  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(axiosConfig.method)) {
    axiosConfig.data = body || {};
  }

  if (axiosConfig.method === 'GET' && params && typeof params === 'object') {
    axiosConfig.params = params;
  }

  const start = Date.now();
  let apiResponse;
  try {
    apiResponse = await axios(axiosConfig);
  } catch (err) {
    apiResponse = err.response || { status: 'ERR', data: { error: err.message }, headers: {} };
  }
  const duration = Date.now() - start;

  return {
    status: apiResponse.status || 'ERR',
    headers: apiResponse.headers || {},
    body: apiResponse.data || {},
    duration
  };
};

// Handles proxy API execution requests, executing target HTTP request and logging to history
export const proxyRequestHandler = async (req, res, next) => {
  try {
    const { url, method, headers, params, body, appliedFix, aiDiagnosis } = req.body;
    const proxyResult = await executeProxyRequest({ url, method, headers, params, body });
    const timelineKey = getTimelineKey(method, url);

    const historyEntry = {
      method,
      url,
      headers: headers || {},
      params: params || {},
      requestBody: body || null,
      responseBody: proxyResult.body,
      status: proxyResult.status,
      duration: proxyResult.duration,
      appliedFix: appliedFix || null,
      aiDiagnosis: aiDiagnosis || null,
      timelineKey,
      time: new Date()
    };

    const savedEntry = await pushHistoryItem(req.userId, historyEntry);

    // Asynchronously index in ChromaDB if GenAI service is available
    const genaiUrl = process.env.GENAI_SERVICE_URL;
    const genaiSecret = process.env.GENAI_API_SECRET;
    if (genaiUrl) {
      axios.post(`${genaiUrl}/rag/index-episode`, {
        userId: String(req.userId || 'guest'),
        method: method.toUpperCase(),
        url,
        failedStatus: proxyResult.status,
        errorSnippet: typeof proxyResult.body === 'object' ? JSON.stringify(proxyResult.body).slice(0, 300) : String(proxyResult.body || '').slice(0, 300),
        rootCauseLayer: String(proxyResult.status).startsWith('2') ? 'Success' : 'General',
        appliedFix: appliedFix || {},
        successStatus: String(proxyResult.status).startsWith('2') ? proxyResult.status : 200,
        successDuration: proxyResult.duration,
        customId: `req_${savedEntry._id || Date.now()}`
      }, {
        headers: { 'x-api-key': genaiSecret },
        timeout: 3000
      }).catch(() => {});
    }

    res.json({
      success: true,
      ...proxyResult,
      historyId: savedEntry._id,
      timelineKey,
    });
  } catch (error) {
    next(error);
  }
};
