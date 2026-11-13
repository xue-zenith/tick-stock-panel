import { useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchMinuteBatchIncremental } from './minuteBatchIncremental'
import { QK } from './queryKeys'

export function useMinuteBatchQuery(
  symbols: string[],
  options: { enabled: boolean; preferLocal: boolean; refetchInterval: number | false },
) {
  const client = useQueryClient()
  const scope = JSON.stringify([symbols, options.preferLocal])
  const fetchedScope = useRef<string | null>(null)
  const enabled = options.enabled && symbols.length > 0
  const query = useQuery({
    queryKey: QK.minuteBatch(),
    queryFn: () => {
      fetchedScope.current = scope
      return fetchMinuteBatchIncremental(client, symbols, options.preferLocal)
    },
    enabled,
    staleTime: 10_000,
    placeholderData: previous => previous,
    refetchInterval: options.refetchInterval,
  })

  const { isFetching, refetch } = query

  useEffect(() => {
    // The cache stays shared and bounded, but changing queryFn alone does not
    // load a new list. Finish existing work, then fetch the latest scope once;
    // genuine empty responses and failures must not cause a request loop.
    if (enabled && !isFetching && fetchedScope.current !== scope) {
      void refetch()
    }
  }, [enabled, scope, isFetching, refetch])

  return query
}
