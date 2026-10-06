import mongoose from 'mongoose';
import axios from 'axios';
import User from '../models/userModel.js';
import { ApiError } from '../utils/ApiError.js';

const genaiUrl = process.env.GENAI_SERVICE_URL;
const genaiApiSecret = process.env.GENAI_API_SECRET;

// Helper to sanitize and bound large payloads before storing in MongoDB
function sanitizePayload(data, maxBytes = 32768) {
  if (data === null || data === undefined) return null;
  try {
    if (typeof data === 'string') {
      return data.length > maxBytes
        ? data.slice(0, maxBytes) + '... [Truncated for storage optimization]'
        : data;
    }
    const str = JSON.stringify(data);
    if (str.length > maxBytes) {
      if (Array.isArray(data)) {
        return data.slice(0, 20).map((item) => {
          if (typeof item === 'object' && item !== null) {
            const preview = {};
            for (const [k, v] of Object.entries(item)) {
              if (['url', 'name', 'method', 'status', '_id', 'time'].includes(k)) {
                preview[k] = v;
              } else if (typeof v === 'string') {
                preview[k] = v.length > 80 ? v.slice(0, 80) + '…' : v;
              } else if (typeof v === 'number' || typeof v === 'boolean') {
                preview[k] = v;
              }
            }
            return preview;
          }
          return item;
        });
      }
      return { _truncated: true, size: str.length, summary: `Payload (${str.length} bytes) was truncated to preserve storage limits.` };
    }
    return data;
  } catch {
    return null;
  }
}

// Appends a new API request history entry to the user's history array (max 50)
export const pushHistoryItem = async (userId, historyEntry) => {
  const newEntry = {
    ...historyEntry,
    requestBody: sanitizePayload(historyEntry.requestBody, 16384),
    responseBody: sanitizePayload(historyEntry.responseBody, 32768),
    _id: new mongoose.Types.ObjectId(),
    time: historyEntry.time || new Date()
  };

  try {
    await User.findByIdAndUpdate(userId, {
      $push: {
        history: {
          $each: [newEntry],
          $slice: -50 // Keep latest 50
        }
      },
      $inc: { reqCount: 1 }
    });
  } catch (err) {
    if (err.message && (err.message.includes('BSONObj size') || err.message.includes('16777216') || err.message.includes('document size'))) {
      console.warn(`Pruning oversized history array for user ${userId}...`);
      const user = await User.findById(userId);
      if (user && Array.isArray(user.history)) {
        const pruned = user.history.slice(-15).map((h) => {
          const obj = h.toObject ? h.toObject() : { ...h };
          return {
            ...obj,
            responseBody: sanitizePayload(obj.responseBody, 4096),
            requestBody: sanitizePayload(obj.requestBody, 2048)
          };
        });
        await User.findByIdAndUpdate(userId, { $set: { history: [...pruned, newEntry] } });
      } else {
        await User.findByIdAndUpdate(userId, { $set: { history: [newEntry] } });
      }
    } else {
      throw err;
    }
  }

  return newEntry;
};

// Appends a new verified resolution episode into the user's RAG memory
export const pushResolutionEpisode = async (userId, episode) => {
  const newEpisode = {
    ...episode,
    _id: new mongoose.Types.ObjectId(),
    timestamp: episode.timestamp || new Date()
  };

  await User.findByIdAndUpdate(userId, {
    $push: {
      resolutionEpisodes: {
        $each: [newEpisode],
        $slice: -100
      }
    }
  });

  return newEpisode;
};

// Retrieves the request execution history for the authenticated user (latest 50 entries)
export const fetchHistoryHandler = async (req, res, next) => {
  try {
    if (!mongoose.Types.ObjectId.isValid(req.userId)) return res.json([]);
    const user = await User.findById(req.userId, { history: { $slice: -50 } });
    if (!user || !Array.isArray(user.history)) return res.json([]);
    res.json(user.history.slice().reverse());
  } catch (error) {
    next(error);
  }
};

// Deletes a single request history entry by ID and synchronizes embedding removal in ChromaDB
export const deleteHistoryHandler = async (req, res, next) => {
  try {
    const { historyId } = req.params;
    const result = await User.findByIdAndUpdate(req.userId, {
      $pull: { history: { _id: historyId } }
    });

    if (!result) {
      return next(new ApiError(404, 'History item not found'));
    }

    if (genaiUrl) {
      axios.post(`${genaiUrl}/rag/delete-episode`, {
        userId: req.userId,
        episodeId: historyId
      }, {
        headers: { 'x-api-key': genaiApiSecret }
      }).catch((err) => console.warn('Non-blocking ChromaDB delete episode notice:', err.message));
    }

    res.json({ success: true, message: 'History item and vector embedding deleted successfully' });
  } catch (error) {
    next(error);
  }
};

// Clears all request history entries and purges the user's vector memory from ChromaDB
export const clearHistoryHandler = async (req, res, next) => {
  try {
    const result = await User.findByIdAndUpdate(req.userId, { $set: { history: [] } });
    if (!result) {
      return next(new ApiError(404, 'User not found'));
    }

    if (genaiUrl) {
      axios.post(`${genaiUrl}/rag/clear-user-memory`, {
        userId: req.userId
      }, {
        headers: { 'x-api-key': genaiApiSecret }
      }).catch((err) => console.warn('Non-blocking ChromaDB clear memory notice:', err.message));
    }

    res.json({ success: true, message: 'History and vector memory cleared successfully' });
  } catch (error) {
    next(error);
  }
};
